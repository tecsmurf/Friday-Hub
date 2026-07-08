"""Friday Hub — Learning package.

Self-improving feedback loop that evaluates task results and stores
scores in long-term memory for future routing improvements.

Functions:
    evaluate_result     Auto-evaluate a task execution result
    submit_feedback     Record manual user feedback (thumbs up/down)
"""

from friday.learning.feedback import evaluate_result, submit_feedback

__all__ = ["evaluate_result", "submit_feedback"]
