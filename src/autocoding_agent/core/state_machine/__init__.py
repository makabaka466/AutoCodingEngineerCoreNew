"""Public lifecycle contracts.

The transition service lives in ``state_machine.machine`` so core model imports do not create a
package initialization cycle.
"""

from autocoding_agent.core.state_machine.models import (
    AgentCommand,
    AgentCommandType,
    FailureClass,
    TaskState,
    TransitionRule,
)

__all__ = [
    "AgentCommand",
    "AgentCommandType",
    "FailureClass",
    "TaskState",
    "TransitionRule",
]
