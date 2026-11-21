from minisweagent.models.utils.cache_control import set_cache_control


def _marked(messages: list[dict]) -> list[bool]:
    return [isinstance(m["content"], list) and "cache_control" in m["content"][0] for m in messages]


def test_last_two_marks_persistent_message_before_transient_one():
    messages = [{"role": "user", "content": f"m{i}"} for i in range(4)]
    assert _marked(set_cache_control(messages, mode="default_end")) == [False, False, False, True]
    assert _marked(set_cache_control(messages, mode="last_two")) == [False, False, True, True]
    assert messages[0]["content"] == "m0"
