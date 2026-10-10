"""Tests for friday.memory.short_term — In-memory TTL store."""




class TestShortTermMemory:
    """Test the short-term memory key-value store."""

    def setup_method(self):
        """Clear memory before each test."""
        from friday.memory import short_term
        short_term.clear()

    def test_put_and_get(self):
        """Basic put/get should store and retrieve values."""
        from friday.memory import short_term

        short_term.put("key1", "value1")
        assert short_term.get("key1") == "value1"

    def test_get_missing_key(self):
        """Getting a missing key should return default."""
        from friday.memory import short_term

        assert short_term.get("nonexistent") is None
        assert short_term.get("nonexistent", "fallback") == "fallback"

    def test_put_overwrites(self):
        """Putting the same key should overwrite the value."""
        from friday.memory import short_term

        short_term.put("key", "old")
        short_term.put("key", "new")
        assert short_term.get("key") == "new"

    def test_put_complex_values(self):
        """Memory should store dicts, lists, and nested structures."""
        from friday.memory import short_term

        data = {"task": "refactor", "agents": ["claude", "codex"], "meta": {"score": 0.9}}
        short_term.put("complex", data)
        retrieved = short_term.get("complex")
        assert retrieved == data
        assert retrieved["agents"][0] == "claude"

    def test_get_recent(self):
        """get_recent should return items sorted by most recent first."""
        from friday.memory import short_term

        short_term.put("a", 1)
        short_term.put("b", 2)
        short_term.put("c", 3)

        recent = short_term.get_recent(2)
        assert len(recent) == 2
        assert recent[0]["key"] == "c"  # Most recent
        assert recent[1]["key"] == "b"

    def test_get_recent_all(self):
        """get_recent with large n should return all entries."""
        from friday.memory import short_term

        short_term.put("x", 10)
        short_term.put("y", 20)

        recent = short_term.get_recent(100)
        assert len(recent) == 2

    def test_conversation_history(self):
        """Conversation should append messages and maintain order."""
        from friday.memory import short_term

        short_term.add_to_conversation("user", "hello")
        short_term.add_to_conversation("assistant", "hi there")
        short_term.add_to_conversation("user", "debug this")

        conv = short_term.get_conversation()
        assert len(conv) == 3
        assert conv[0]["role"] == "user"
        assert conv[0]["content"] == "hello"
        assert conv[1]["role"] == "assistant"
        assert conv[2]["content"] == "debug this"

    def test_conversation_truncation(self):
        """Conversation should be truncated to last 20 messages."""
        from friday.memory import short_term

        for i in range(25):
            short_term.add_to_conversation("user", f"message {i}")

        conv = short_term.get_conversation()
        assert len(conv) == 20
        assert conv[0]["content"] == "message 5"  # messages 0-24 sent; last 20 are 5-24

    def test_clear(self):
        """clear() should remove all entries."""
        from friday.memory import short_term

        short_term.put("a", 1)
        short_term.put("b", 2)
        short_term.clear()

        assert short_term.get("a") is None
        assert short_term.get("b") is None
        assert short_term.get_recent(100) == []
