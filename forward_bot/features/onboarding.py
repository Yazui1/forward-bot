from __future__ import annotations

from dataclasses import dataclass

from forward_bot.db.repository import Repository, User
from forward_bot.utils import human_seconds


@dataclass(frozen=True, slots=True)
class OnboardingQuestion:
    question: str
    answer: str


def onboarding_questions(repo: Repository) -> list[OnboardingQuestion]:
    questions: list[OnboardingQuestion] = []
    for item in repo.list_ack_rules():
        questions.append(OnboardingQuestion(question=item["question"], answer=item["answer"]))
    return questions


def requires_onboarding_answers(user: User, repo: Repository) -> bool:
    return bool(
        onboarding_questions(repo)
        and not user.is_mod_or_admin
        and not user.onboarding_acknowledged
    )


def current_onboarding_question(user: User, repo: Repository) -> OnboardingQuestion | None:
    questions = onboarding_questions(repo)
    if not questions:
        return None
    index = max(0, min(user.onboarding_question_index, len(questions) - 1))
    return questions[index]


def onboarding_prompt(user: User, repo: Repository) -> str:
    question = current_onboarding_question(user, repo)
    if question is None:
        return ""
    return question.question


def onboarding_complete_message(user: User) -> str:
    if user.active_cooldown_seconds > 0:
        return f"Onboarding acknowledged. Cooldown still active: {human_seconds(user.active_cooldown_seconds)}."
    return "Onboarding acknowledged. You can use the bot now."
