"""共用的工程知识检索入口；检索失败时允许主任务安全降级。"""

from __future__ import annotations

from typing import Protocol

from autocoding_agent.core.models import AgentEvent, EventType
from autocoding_agent.core.progress import (
    ProgressEvent,
    ProgressPhase,
    ProgressSink,
    ProgressWorkflow,
    emit_progress,
)
from autocoding_agent.knowledge_rag.models import KnowledgeDomain
from autocoding_agent.knowledge_rag.ports import KnowledgeRetriever
from autocoding_agent.knowledge_rag.service import workspace_id_for


class KnowledgeAggregate(Protocol):
    id: str
    workspace: str
    project: str | None
    events: list[AgentEvent]


def retrieve_knowledge_context(
    retriever: KnowledgeRetriever | None,
    session: KnowledgeAggregate,
    query: str,
    *,
    domain: KnowledgeDomain,
    workflow: ProgressWorkflow,
    command_id: str,
    progress_sink: ProgressSink | None,
) -> str:
    """检索有界提示词片段，并审计成功结果或失败降级。"""

    if retriever is None:
        return ""
    emit_progress(
        progress_sink,
        ProgressEvent.for_phase(
            workflow,
            ProgressPhase.RETRIEVING_KNOWLEDGE,
            task_id=session.id,
        ),
    )
    try:
        result = retriever.retrieve(
            query,
            domain=domain,
            project=session.project,
            workspace_id=workspace_id_for(session.workspace),
        )
    except Exception as exc:
        session.events.append(
            AgentEvent(
                type=EventType.KNOWLEDGE_RETRIEVAL_FAILED,
                message="RAG knowledge retrieval failed; continuing without retrieved knowledge.",
                actor="host",
                command_id=command_id,
                data={
                    "error": " ".join(str(exc).split())[:800],
                    "workflow": workflow.value,
                },
            )
        )
        return ""
    session.events.append(
        AgentEvent(
            type=EventType.KNOWLEDGE_RETRIEVED,
            message=f"Retrieved {len(result.hits)} manually indexed knowledge chunks.",
            actor="host",
            command_id=command_id,
            data={
                "count": len(result.hits),
                "embedding_model": result.embedding_model,
                "simulated": result.simulated,
                "workflow": workflow.value,
                "chunks": [
                    {"chunk_id": item.chunk_id, "source": item.source_path}
                    for item in result.hits
                ],
            },
        )
    )
    return result.prompt_context()
