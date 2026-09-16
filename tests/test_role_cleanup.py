from __future__ import annotations

import unittest
from types import SimpleNamespace

from octocop.cogs.roles import RoleCleanupCog
from octotracker.config import Config

LEGACY = 1547037223558451291
AUTO = 1547371277474603028
SWEEP_ONLY = 1547038342661804194
UNRELATED = 999


class _Role:
    def __init__(self, role_id: int):
        self.id = role_id
        self.mention = f"<@&{role_id}>"

    def __eq__(self, other):
        return isinstance(other, _Role) and other.id == self.id

    def __hash__(self):
        return hash(self.id)


class _Guild:
    def __init__(self, guild_id: int, roles: dict[int, _Role]):
        self.id = guild_id
        self._roles = roles

    def get_role(self, role_id: int):
        return self._roles.get(role_id)


class _Member:
    def __init__(self, guild: _Guild, member_id: int, role_ids: list[int]):
        self.guild = guild
        self.id = member_id
        self.roles = [guild.get_role(role_id) for role_id in role_ids]
        self.removed: list[tuple[_Role, str]] = []

    async def remove_roles(self, role, *, reason=None):
        self.removed.append((role, reason))
        self.roles = [existing for existing in self.roles if existing != role]


def _config(**overrides) -> Config:
    return Config(
        token="test",
        guild_id=123,
        status_channel_id=None,
        alert_channel_id=None,
        announcement_channel_id=None,
        **overrides,
    )


class RoleCleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.roles = {role_id: _Role(role_id) for role_id in (LEGACY, AUTO, SWEEP_ONLY, UNRELATED)}
        self.guild = _Guild(123, self.roles)
        self.cog = RoleCleanupCog(SimpleNamespace(config=_config()))

    def test_defaults_match_the_requested_role_ids(self) -> None:
        config = _config()
        self.assertEqual(config.role_cleanup_remove_role_id, LEGACY)
        self.assertEqual(config.role_cleanup_auto_trigger_role_ids, frozenset({AUTO}))
        self.assertEqual(
            config.role_cleanup_sweep_trigger_role_ids,
            frozenset({1547038342661804194, 1547038337297154078, 1547038296025333880, AUTO}),
        )

    async def test_gaining_the_trigger_role_removes_the_legacy_role(self) -> None:
        before = _Member(self.guild, 1, [LEGACY])
        after = _Member(self.guild, 1, [LEGACY, AUTO])
        await self.cog.on_member_update(before, after)
        self.assertEqual([role.id for role, _ in after.removed], [LEGACY])
        self.assertNotIn(self.roles[LEGACY], after.roles)

    async def test_regaining_legacy_while_holding_trigger_removes_it_again(self) -> None:
        before = _Member(self.guild, 1, [AUTO])
        after = _Member(self.guild, 1, [AUTO, LEGACY])
        await self.cog.on_member_update(before, after)
        self.assertEqual([role.id for role, _ in after.removed], [LEGACY])

    async def test_unrelated_updates_and_other_guilds_are_ignored(self) -> None:
        before = _Member(self.guild, 1, [LEGACY, AUTO])
        after = _Member(self.guild, 1, [LEGACY, AUTO, UNRELATED])
        await self.cog.on_member_update(before, after)
        self.assertEqual(after.removed, [])

        # The sweep-only roles do not trigger the automatic path.
        before = _Member(self.guild, 2, [LEGACY])
        after = _Member(self.guild, 2, [LEGACY, SWEEP_ONLY])
        await self.cog.on_member_update(before, after)
        self.assertEqual(after.removed, [])

        other = _Guild(456, self.roles)
        before = _Member(other, 3, [LEGACY])
        after = _Member(other, 3, [LEGACY, AUTO])
        await self.cog.on_member_update(before, after)
        self.assertEqual(after.removed, [])

    async def test_member_without_legacy_role_is_left_alone(self) -> None:
        before = _Member(self.guild, 1, [])
        after = _Member(self.guild, 1, [AUTO])
        await self.cog.on_member_update(before, after)
        self.assertEqual(after.removed, [])

    def test_sweep_trigger_check_covers_all_four_roles(self) -> None:
        triggers = self.cog.bot.config.role_cleanup_sweep_trigger_role_ids
        self.assertTrue(self.cog._has_any(_Member(self.guild, 1, [SWEEP_ONLY]), triggers))
        self.assertTrue(self.cog._has_any(_Member(self.guild, 1, [AUTO]), triggers))
        self.assertFalse(self.cog._has_any(_Member(self.guild, 1, [UNRELATED, LEGACY]), triggers))


if __name__ == "__main__":
    unittest.main()
