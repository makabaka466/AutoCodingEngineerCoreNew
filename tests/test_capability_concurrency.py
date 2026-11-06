"""多个进程同时保存经验时不能破坏索引，同路径的不同项目不共用模型知识视图。"""

import json
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import pytest

from autocoding_agent.adapters.capability_store import CapabilityStore
from autocoding_agent.core.models import AgentDecision, AgentSession, AgentStatus
from autocoding_agent.incident.capability_store import IncidentCapabilityStore
from autocoding_agent.incident.models import IncidentDecision, IncidentSession, LocatedPage


def _record(arguments):
    data_dir, workspace, workflow, project, number = arguments
    message = f"{project} task {number}"
    if workflow == "development":
        store = CapabilityStore(Path(data_dir))
        session = AgentSession(workspace=workspace, project=project, goal=message)
        decision = AgentDecision(status=AgentStatus.COMPLETED, message=message)
    else:
        store = IncidentCapabilityStore(Path(data_dir))
        session = IncidentSession(workspace=workspace, project=project, problem=message)
        decision = IncidentDecision(
            status="completed", message=message, diagnosis=message,
            recommended_actions=["核对结果"],
            page=LocatedPage(
                name="测试页面", source_paths=["page.cs"], matched_evidence=["精确名称"],
                explanation="已有入口证据",
            ),
        )
    receipt = store.record(session, decision, "test-model")
    return receipt.document_path, receipt.index_path


@pytest.mark.parametrize("workflow", ["development", "incident"])
def test_multiple_processes_preserve_all_capability_records(tmp_path, workflow):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    arguments = [(str(tmp_path / "state"), str(workspace), workflow, "project-A", number)
                 for number in range(8)]
    with ProcessPoolExecutor(
        max_workers=3, mp_context=multiprocessing.get_context("spawn"),
    ) as executor:
        receipts = list(executor.map(_record, arguments))
    index = Path(receipts[0][1]).read_text(encoding="utf-8")
    for document, _ in receipts:
        assert index.count(f"capabilities/{Path(document).name}") == 1
        assert Path(document).read_text(encoding="utf-8")
    archive = Path(receipts[0][1]).parent
    assert len(list((archive / "tasks").glob("*.json"))) == 8
    assert not list(archive.rglob("*.tmp"))
    for record in (archive / "tasks").glob("*.json"):
        assert json.loads(record.read_text(encoding="utf-8"))["project"] == "project-A"


@pytest.mark.parametrize("workflow", ["development", "incident"])
def test_same_workspace_projects_have_separate_stable_knowledge_views(tmp_path, workflow):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    knowledge = tmp_path / workflow
    for project in ("project-A", "project-B"):
        source = knowledge / project / f"{project}.md"
        source.parent.mkdir(parents=True)
        source.write_text(f"# {project}\n{project}专属知识", encoding="utf-8")
        _record((str(tmp_path / "state"), str(workspace), workflow, project, 1))
    store_type = CapabilityStore if workflow == "development" else IncidentCapabilityStore
    store = store_type(tmp_path / "state", knowledge)
    with ThreadPoolExecutor(max_workers=4) as executor:
        views = list(executor.map(
            lambda project: store.prepare(workspace, project), ["project-A", "project-B"] * 4,
        ))
    assert len(set(views)) == 2
    for project, view in zip(("project-A", "project-B"), views[:2], strict=True):
        texts = "\n".join(path.read_text(encoding="utf-8") for path in view.rglob("*.md"))
        other = "project-B" if project == "project-A" else "project-A"
        assert f"{project}专属知识" in texts
        assert f"{project} task 1" in texts
        assert other not in texts
        assert len(list((view / "capabilities").glob("*.md"))) == 1
    # 视图复制不搬走旧归档，RAG仍能使用原路径发现两项目经验。
    archive = store._workspace_dir(workspace)
    assert len(list((archive / "capabilities").glob("*.md"))) == 2
