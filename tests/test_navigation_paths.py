"""确定性路径解析的项目边界、多候选及命名空间规则回归。"""

import pytest
from pydantic import ValidationError

from autocoding_agent.navigation_paths import PathMappingRule, resolve_navigation_path


def test_namespace_uses_explicit_project_root_mapping(tmp_path):
    source = tmp_path / "zwqtmes/MESClient/CKClient/DeviceUpload.cs"
    source.parent.mkdir(parents=True)
    source.write_text("class DeviceUpload {}", encoding="utf-8")
    rule = PathMappingRule(
        namespace_prefix="Ckhy.MES.Client.CKClient", relative_directory="zwqtmes/MESClient/CKClient"
    )
    result = resolve_navigation_path(tmp_path, "Ckhy.MES.Client.CKClient.DeviceUpload", [rule])
    assert result.kind == "namespace"
    assert result.candidate_paths == ["zwqtmes/MESClient/CKClient/DeviceUpload.cs"]
    assert not result.missing
    assert not result.conflicts


@pytest.mark.parametrize("location", ["src/Page.cs", "src\\Page.cs"])
def test_relative_location_is_joined_once(tmp_path, location):
    source = tmp_path / "src/Page.cs"
    source.parent.mkdir()
    source.touch()
    result = resolve_navigation_path(tmp_path, location)
    assert result.candidate_paths == ["src/Page.cs"]
    assert result.attempted_paths == ["src/Page.cs"]


def test_workspace_absolute_file_is_normalized_but_outside_file_is_rejected(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    inside = workspace / "Page.cs"
    outside = tmp_path / "Outside.cs"
    inside.touch()
    outside.touch()
    assert resolve_navigation_path(workspace, str(inside)).candidate_paths == ["Page.cs"]
    escaped = resolve_navigation_path(workspace, str(outside))
    assert not escaped.candidate_paths and escaped.conflicts
    assert not escaped.attempted_paths


@pytest.mark.parametrize(
    "location",
    [
        "../Outside.cs",
        "src/../../Outside.cs",
        "..\\Outside.cs",
        "C:Outside.cs",
        "src/*.cs",
        "src/Page.cs:Zone.Identifier",
        "src/Page.cs\x00",
        "https://example.com/Page.cs",
    ],
)
def test_untrusted_locations_cannot_escape_or_trigger_broad_search(tmp_path, location):
    result = resolve_navigation_path(tmp_path, location)
    assert not result.candidate_paths
    assert result.conflicts


@pytest.mark.parametrize("directory", ["../src", "/src", "C:/src", "**/src", "src:other"])
def test_mapping_config_rejects_non_relative_or_wildcard_directory(directory):
    with pytest.raises(ValidationError):
        PathMappingRule(namespace_prefix="Company.Client", relative_directory=directory)


@pytest.mark.parametrize(
    "prefix", ["Company/Client", "Company.*", "Company..Client", "Company.1Client"]
)
def test_mapping_config_rejects_non_namespace_prefix(prefix):
    with pytest.raises(ValidationError):
        PathMappingRule(namespace_prefix=prefix, relative_directory="src")


@pytest.mark.parametrize("extension", ["cs", ".cs/../x", ".*", ".cs:other"])
def test_mapping_config_rejects_non_literal_extension(extension):
    with pytest.raises(ValidationError):
        PathMappingRule(
            namespace_prefix="Company.Client", relative_directory="src", extension=extension
        )


def test_assembly_qualified_type_is_parsed_without_loading_assembly(tmp_path):
    (tmp_path / "Page.cs").touch()
    rule = PathMappingRule(namespace_prefix="Company.Client", relative_directory=".")
    result = resolve_navigation_path(
        tmp_path,
        "Company.Client.Page, Company.Client, Version=1.2.3.4, "
        "Culture=neutral, PublicKeyToken=null",
        [rule],
    )
    assert result.candidate_paths == ["Page.cs"]
    assert not result.conflicts


@pytest.mark.parametrize(
    "location",
    [
        "Company.Client.Page, ../Client.dll",
        "Company.Client.Page, Client, CodeBase=file:///private/file",
        "Company.Client.Page, Client, Version=1.0, Version=2.0",
        "Company.Client.Page, Client, PublicKeyToken=invalid",
        "Company.Client.List`1[[System.String, mscorlib]], Client",
    ],
)
def test_assembly_qualifiers_with_paths_or_unsupported_formats_are_rejected(tmp_path, location):
    rule = PathMappingRule(namespace_prefix="Company.Client", relative_directory=".")
    result = resolve_navigation_path(tmp_path, location, [rule])
    assert result.conflicts
    assert not result.candidate_paths
    assert not result.attempted_paths


def test_no_rule_does_not_guess_from_similar_existing_file(tmp_path):
    (tmp_path / "Upload.cs").touch()
    result = resolve_navigation_path(tmp_path, "Company.Client.Upload")
    assert not result.candidate_paths
    assert result.missing
    assert result.attempted_paths == ["Company.Client.Upload"]
    qualified = resolve_navigation_path(tmp_path, "Company.Client.Upload, Company.Client")
    assert qualified.kind == "namespace"
    assert qualified.missing
    assert not qualified.attempted_paths


def test_missing_mapped_file_is_not_found_by_recursively_scanning_other_directories(tmp_path):
    unrelated = tmp_path / "old/source/Upload.cs"
    unrelated.parent.mkdir(parents=True)
    unrelated.touch()
    rule = PathMappingRule(namespace_prefix="Company.Client", relative_directory="new/source")
    result = resolve_navigation_path(tmp_path, "Company.Client.Upload", [rule])
    assert not result.candidate_paths
    assert result.attempted_paths == ["new/source/Upload.cs"]
    assert "new/source/Upload.cs" in result.missing[0]


def test_all_existing_rule_candidates_are_reported_instead_of_choosing_first(tmp_path):
    rules = []
    for folder in ["one", "two"]:
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "Page.cs").touch()
        rules.append(PathMappingRule(namespace_prefix="Company.Client", relative_directory=folder))
    result = resolve_navigation_path(tmp_path, "Company.Client.Page", rules)
    assert result.candidate_paths == ["one/Page.cs", "two/Page.cs"]
    assert result.conflicts
    duplicated = resolve_navigation_path(tmp_path, "Company.Client.Page", [rules[0], rules[0]])
    assert duplicated.candidate_paths == ["one/Page.cs"]
    assert not duplicated.conflicts


def test_subnamespace_segments_and_custom_extension_are_deterministic(tmp_path):
    source = tmp_path / "src/Forms/Order.vb"
    source.parent.mkdir(parents=True)
    source.touch()
    rule = PathMappingRule(
        namespace_prefix="Company.Client", relative_directory="src", extension=".vb"
    )
    result = resolve_navigation_path(tmp_path, "Company.Client.Forms.Order", [rule])
    assert result.candidate_paths == ["src/Forms/Order.vb"]
    assert not resolve_navigation_path(
        tmp_path, "Company.ClientExtra.Forms.Order", [rule]
    ).candidate_paths


def test_symlink_escape_is_rejected_for_direct_and_mapped_locations(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "Page.cs").touch()
    try:
        (workspace / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前系统账户不能创建目录符号链接")
    rule = PathMappingRule(namespace_prefix="Company.Client", relative_directory="linked")
    for location in ["linked/Page.cs", "Company.Client.Page"]:
        result = resolve_navigation_path(workspace, location, [rule])
        assert result.conflicts
        assert not result.candidate_paths
        assert not result.attempted_paths


def test_invalid_workspace_and_empty_location_produce_explanations(tmp_path):
    absent = resolve_navigation_path(tmp_path / "absent", "Page.cs")
    assert absent.missing and not absent.candidate_paths
    empty = resolve_navigation_path(tmp_path, "  ")
    assert empty.conflicts and not empty.candidate_paths
