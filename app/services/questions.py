"""Вопросы участников организаторам и ответы на них."""
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MediaType, Participant, Question, QuestionStatus


async def create_question(
    session: AsyncSession,
    participant: Participant,
    *,
    text: str | None,
    media_type: MediaType | None = None,
    media_file_id: str | None = None,
) -> Question:
    question = Question(participant_id=participant.id, text=text, media_type=media_type, media_file_id=media_file_id)
    session.add(question)
    await session.flush()
    return question


async def find_by_admin_message(session: AsyncSession, message_id: int) -> Question | None:
    return await session.scalar(select(Question).where(Question.admin_chat_message_id == message_id))


async def mark_answered(session: AsyncSession, question_id: int, answer: str, admin_id: int) -> bool:
    """Помечает вопрос отвеченным. Повторный ответ тоже разрешён (обновляет текст) — False, если вопроса нет."""
    result = await session.execute(
        update(Question)
        .where(Question.id == question_id)
        .values(
            status=QuestionStatus.ANSWERED,
            answer_text=answer,
            answered_by=admin_id,
            answered_at=datetime.now(UTC),
        )
    )
    return result.rowcount > 0
