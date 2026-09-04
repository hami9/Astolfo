from astolfo.learning import MAX_CHAT_STYLE, MAX_PEOPLE, MAX_PERSON_STYLE, Style


def test_learns_a_chat_line_and_people_lines():
    style = Style()
    assert style.learn(chat="Finglish, short messages", people={"Reza": "only jokes"})
    assert style.chat == "Finglish, short messages"
    assert style.note_for("reza") == "only jokes"


def test_learning_the_same_thing_twice_changes_nothing():
    style = Style()
    style.learn(chat="short messages", people={"Reza": "only jokes"})
    assert not style.learn(chat="short messages", people={"Reza": "only jokes"})


def test_only_the_people_in_this_turn_are_sent():
    """A group of twenty must not pay for twenty lines on every message."""
    style = Style()
    style.learn(chat="Finglish", people={"Reza": "jokes", "Sara": "asks real questions"})

    block = style.for_turn("Sara")
    assert "Sara: asks real questions" in block
    assert "Reza" not in block
    assert "this chat: Finglish" in block

    both = style.for_turn("Sara", "Reza")
    assert "Reza: jokes" in both and "Sara" in both


def test_a_name_repeated_in_one_turn_is_sent_once():
    style = Style()
    style.learn(people={"Reza": "jokes"})
    assert style.for_turn("Reza", "reza", "").count("jokes") == 1


def test_lines_are_capped_so_the_prompt_cannot_grow():
    style = Style()
    style.learn(chat="x" * 500, people={"Reza": "y" * 500})
    assert len(style.chat) == MAX_CHAT_STYLE
    assert len(style.note_for("Reza")) == MAX_PERSON_STYLE


def test_the_oldest_person_is_forgotten_first():
    style = Style()
    for n in range(MAX_PEOPLE + 3):
        style.learn(people={f"person{n}": f"note {n}"})
    assert len(style.people) == MAX_PEOPLE
    assert not style.note_for("person0")
    assert style.note_for(f"person{MAX_PEOPLE + 2}")


def test_a_person_seen_again_is_not_the_oldest_any_more():
    style = Style()
    style.learn(people={"Reza": "jokes"})
    for n in range(MAX_PEOPLE - 1):
        style.learn(people={f"person{n}": "note"})
    style.learn(people={"Reza": "jokes, and asks about football"})
    style.learn(people={"newcomer": "note"})
    assert style.note_for("Reza"), "the one who just spoke was dropped instead"


def test_empty_observations_are_ignored():
    style = Style()
    assert not style.learn(chat="", people={"Reza": "", "": "something"})
    assert not style


def test_survives_a_round_trip_through_the_database_column():
    style = Style()
    style.learn(chat="می‌گن کوتاه بنویس", people={"رضا": "شوخی می‌کنه"})
    again = Style.loads(style.dumps())
    assert again.chat == style.chat
    assert again.note_for("رضا") == "شوخی می‌کنه"


def test_a_broken_stored_value_costs_the_style_not_the_chat():
    assert not Style.loads("{not json")
    assert not Style.loads("[1, 2]")
    assert not Style.loads(None)
    assert not Style.loads("")
    assert Style().dumps() == ""


def test_forget_starts_over():
    style = Style()
    style.learn(chat="something", people={"Reza": "jokes"})
    style.forget()
    assert not style
    assert style.summary() == "nothing learned yet"
