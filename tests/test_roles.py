from types import SimpleNamespace

from astolfo.roles import ADMIN, MEMBER, OWNER, Roles, Roster, standing


class _Bot:
    """Answers getChatAdministrators, and counts how often it is asked."""

    def __init__(self, members=None, fails: bool = False):
        self.members = members or []
        self.fails = fails
        self.asked = 0

    async def get_chat_administrators(self, chat_id):
        self.asked += 1
        if self.fails:
            raise RuntimeError("not a group")
        return self.members


def _member(user_id: int, status: str = "administrator"):
    return SimpleNamespace(user=SimpleNamespace(id=user_id), status=status)


async def test_it_learns_who_runs_the_group():
    bot = _Bot([_member(1, "creator"), _member(2), _member(999)])
    roster = await Roles().of(bot, -100, bot_id=999)

    assert roster.role(1) == OWNER
    assert roster.role(2) == ADMIN
    assert roster.role(3) == MEMBER
    assert roster.bot_is_admin is True


async def test_the_answer_is_cached():
    """It is a network call on every message otherwise."""
    bot = _Bot([_member(1)])
    roles = Roles()
    await roles.of(bot, -100)
    await roles.of(bot, -100)
    assert bot.asked == 1

    roles.forget(-100)
    await roles.of(bot, -100)
    assert bot.asked == 2


async def test_a_chat_that_will_not_say_costs_nothing():
    """A private chat has no admins, and a failure must not cost the turn."""
    roster = await Roles().of(_Bot(fails=True), -100)
    assert roster.admins == set()
    assert standing(roster, sender_id=1, sender="Reza") == ""


async def test_a_failure_keeps_what_was_already_known():
    bot = _Bot([_member(1)])
    roles = Roles(ttl=0)
    await roles.of(bot, -100)

    bot.fails = True
    roster = await roles.of(bot, -100)
    assert roster.admins == {1}, "a blip must not forget who the admins are"


def test_the_prompt_line_names_the_standing():
    roster = Roster(admins={1, 2}, owner_id=1)
    assert "owns this group" in standing(roster, sender_id=1, sender="Reza")
    assert "one of this group's admins" in standing(roster, sender_id=2, sender="Sara")

    plain = standing(roster, sender_id=3, sender="Mahdi")
    assert "Mahdi" not in plain, "a member needs no introduction"
    assert "plain member here with no powers" in plain


def test_it_is_told_to_sit_on_its_own_powers():
    roster = Roster(admins={1, 999}, bot_is_admin=True)
    line = standing(roster, sender_id=5, sender="Mahdi")
    assert "never use them" in line


def test_nothing_is_said_when_nothing_is_known():
    assert standing(None, sender_id=1, sender="Reza") == ""
    assert standing(Roster(), sender_id=1, sender="Reza") == ""
