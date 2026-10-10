"""
Feedback & Learning Loop — Self-improving routing.

After each task:
  1. Auto-evaluate the result quality (was output meaningful?)
  2. Store feedback in long-term memory
  3. Future routing decisions query past feedback to improve

Over time, the system learns which agents are best for which tasks.
"""

from friday.config import config
from friday.memory import long_term

_learn_cfg = config.get("learning", {})
_ENABLED = _learn_cfg.get("enabled", True)
_AUTO_EVAL = _learn_cfg.get("auto_evaluate", True)


async def evaluate_result(task_id: str, prompt: str, exec_result) -> dict | None:
    """
    Auto-evaluate a task result and store feedback.

    Simple heuristic evaluation (no LLM call to save tokens):
      - Success + non-empty output = positive
      - Failure or empty output = negative
      - Long output with code = likely high quality
    """
    if not _ENABLED:
        return None

    score = 0.0
    reason = ""

    if exec_result.success and exec_result.output:
        output_len = len(exec_result.output)
        if output_len > 500:
            score = 0.9
            reason = "Substantial output produced"
        elif output_len > 100:
            score = 0.7
            reason = "Moderate output produced"
        else:
            score = 0.5
            reason = "Brief output"
    elif exec_result.success:
        score = 0.3
        reason = "Completed but empty output"
    else:
        score = 0.1
        reason = f"Failed: {exec_result.error or 'unknown error'}"

    feedback = {
        "task_id": task_id,
        "agent": exec_result.agent,
        "score": score,
        "reason": reason,
        "auto": True,
    }

    # Store in long-term memory for future routing decisions
    try:
        long_term.store(
            task_id=f"feedback_{task_id}",
            prompt=f"Feedback for: {prompt[:100]}",
            output=f"Agent {exec_result.agent} scored {score}: {reason}",
            agent=exec_result.agent,
            metadata={
                "type": "feedback",
                "score": str(score),
                "original_task": task_id,
            },
        )
    except Exception:
        pass

    return feedback


def submit_feedback(task_id: str, thumbs_up: bool, comment: str = "") -> None:
    """Manual user feedback (thumbs up/down).

    Stores a feedback record and tags the original task with the rating, so
    the router's memory context shows how the user judged that agent's work.

    Raises:
        Exception: when long-term memory is unavailable.
    """
    score = 0.9 if thumbs_up else 0.2
    long_term.update_metadata(task_id, {"user_rating": "good" if thumbs_up else "bad"})
    long_term.store(
        task_id=f"user_feedback_{task_id}",
        prompt=f"User feedback for task {task_id}",
        output=f"{'👍' if thumbs_up else '👎'} {comment}",
        agent="user",
        metadata={
            "type": "user_feedback",
            "score": str(score),
            "thumbs_up": str(thumbs_up),
            "comment": comment,
            "original_task": task_id,
        },
    )
