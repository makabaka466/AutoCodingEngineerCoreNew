"""以显式项目规则解析菜单位置；只返回文件候选，不把路径存在当作入口身份确认。"""

from __future__ import annotations

import re
from pathlib import Path, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class PathMappingRule(BaseModel):
    """命名空间前缀与项目根目录下源码目录的对应关系。"""

    namespace_prefix: str = Field(
        min_length=1, max_length=240, description="大小写敏感的 .NET 命名空间前缀。"
    )
    relative_directory: str = Field(
        min_length=1, max_length=500, description="相对项目根目录的源码目录，允许 . 表示根目录。"
    )
    extension: str = Field(
        default=".cs", min_length=2, max_length=16, description="源码文件扩展名，例如 .cs 或 .vb。"
    )

    @field_validator("namespace_prefix")
    @classmethod
    def validate_namespace(cls, value: str) -> str:
        value = value.strip().rstrip(".")
        if not value or any(not segment.isidentifier() for segment in value.split(".")):
            raise ValueError("命名空间前缀必须由有效标识符组成，不接受路径或通配符。")
        return value

    @field_validator("relative_directory")
    @classmethod
    def validate_directory(cls, value: str) -> str:
        return _relative_path(value.strip()).as_posix()

    @field_validator("extension")
    @classmethod
    def validate_extension(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"\.[A-Za-z0-9]+", value):
            raise ValueError("扩展名必须是点号加字母或数字，不接受目录或通配符。")
        return value


class NavigationPathResolution(BaseModel):
    """宿主可核对的位置解析结果；所有候选仍需结合用户线索和源码确认身份。"""

    location: str = Field(description="数据库返回的原始位置标识。")
    kind: Literal["relative_path", "namespace", "unsupported"] = Field(
        default="unsupported", description="按显式规则识别的位置形式。"
    )
    candidate_paths: list[str] = Field(
        default_factory=list, description="实际存在且位于项目根目录内的相对文件路径。"
    )
    attempted_paths: list[str] = Field(
        default_factory=list, description="按规则尝试的相对文件路径，不含工作区外目标。"
    )
    missing: list[str] = Field(
        default_factory=list, description="缺少映射配置、文件或可解析信息的说明。"
    )
    conflicts: list[str] = Field(
        default_factory=list, description="不安全路径、多个文件候选或格式冲突的说明。"
    )


def _relative_path(value: str) -> Path:
    """规则目录和菜单相对路径共用边界校验，不能规范化后再吞掉父目录跳转。"""
    windows = PureWindowsPath(value)
    if (
        not value
        or windows.drive
        or windows.root
        or ".." in windows.parts
        or any(ord(character) < 32 for character in value)
        or any(character in value for character in ':*?<>|"')
    ):
        raise ValueError("位置必须是无父目录跳转、盘符和通配符的项目相对路径。")
    return Path(value.replace("\\", "/"))


def resolve_navigation_path(
    workspace: str | Path,
    location: str,
    rules: list[PathMappingRule] | tuple[PathMappingRule, ...] = (),
) -> NavigationPathResolution:
    """只检查直接路径及规则生成的精确文件，不递归扫描，也不选择相似文件。

    .NET 程序集限定名只做文本解析，绝不加载程序集；多个规则命中的实际文件全部返回。
    工作区内绝对路径会归一化为相对路径；工作区外路径与链接逃逸被拒绝。
    """
    result = NavigationPathResolution(location=location)
    try:
        root = Path(workspace).expanduser().resolve()
        root_exists = root.is_dir()
    except (OSError, RuntimeError, ValueError):
        root_exists = False
    if not root_exists:
        result.missing.append("配置的项目根目录不存在或不是目录。")
        return result
    value = location.strip()
    if not value or len(value) > 1000 or any(ord(character) < 32 for character in value):
        result.conflicts.append("位置为空、过长或包含控制字符，无法安全解析。")
        return result

    # 普通程序集名以及常见限定元数据可以剥离；路径、代码地址和复杂泛型保持未解析。
    qualified = "," in value
    if qualified:
        fields = [part.strip() for part in value.split(",")]
        metadata = {
            "Version": r"[0-9]+(?:\.[0-9]+){1,3}",
            "Culture": r"[A-Za-z0-9_-]+",
            "PublicKeyToken": r"(?:null|[A-Fa-f0-9]{16})",
            "ProcessorArchitecture": r"[A-Za-z0-9_]+",
        }
        valid = len(fields) >= 2 and bool(re.fullmatch(r"[A-Za-z0-9_.-]+", fields[1]))
        seen: set[str] = set()
        for field in fields[2:]:
            key, separator, detail = field.partition("=")
            key, detail = key.strip(), detail.strip()
            if (
                not separator
                or key not in metadata
                or key in seen
                or not re.fullmatch(metadata.get(key, r"(?!)"), detail)
            ):
                valid = False
            seen.add(key)
        value = fields[0]
        if not valid or any(not segment.isidentifier() for segment in value.split(".")):
            result.conflicts.append("程序集限定名包含不支持或不安全的格式；未加载程序集。")
            return result

    matching = [rule for rule in rules if value.startswith(rule.namespace_prefix + ".")]
    paths: list[Path] = []
    if matching:
        result.kind = "namespace"
        for rule in matching:
            suffix = value[len(rule.namespace_prefix) + 1 :]
            segments = suffix.split(".")
            if any(not segment.isidentifier() for segment in segments):
                result.conflicts.append("命名空间后缀不是简单类型名，不能推断源码文件。")
                return result
            paths.append(
                Path(rule.relative_directory) / Path(*segments).with_suffix(rule.extension)
            )
    elif qualified:
        result.kind = "namespace"
        result.missing.append("程序集限定类型没有匹配的命名空间路径规则。")
        return result
    else:
        result.kind = "relative_path"
        try:
            absolute = Path(value)
            if absolute.is_absolute():
                # 先校验原始组件，防止含 .. 的绝对位置被 resolve 悄悄接受。
                if ".." in PureWindowsPath(value).parts:
                    raise ValueError("位置不能包含父目录跳转。")
                paths.append(_relative_path(absolute.relative_to(root).as_posix()))
            else:
                paths.append(_relative_path(value))
        except ValueError:
            result.conflicts.append("位置不是安全的项目内路径，也未匹配命名空间规则。")
            return result

    for relative in dict.fromkeys(paths):
        try:
            target = (root / relative).resolve()
            if not target.is_relative_to(root):
                result.conflicts.append("候选路径经过链接或配置越出项目根目录，已拒绝。")
                continue
            attempted = relative.as_posix()
            result.attempted_paths.append(attempted)
            if not target.is_file():
                result.missing.append(f"未找到文件：{attempted}")
                continue
            candidate = target.relative_to(root).as_posix()
            if candidate not in result.candidate_paths:
                result.candidate_paths.append(candidate)
        except (OSError, RuntimeError, ValueError):
            result.conflicts.append("候选路径无法安全访问，已停止解析该位置。")
    if (
        not matching
        and not result.candidate_paths
        and not result.conflicts
        and "." in value
        and all(segment.isidentifier() for segment in value.split("."))
    ):
        result.missing.append("若该位置是命名空间类型，需要配置前缀与源码目录映射；不会全仓猜测。")
    if len(result.candidate_paths) > 1:
        result.conflicts.append("存在多个实际文件候选，需核对映射规则和源码身份，不能自动选取。")
    return result
