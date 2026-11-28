from dataclasses import dataclass, field

# One gateway's drained counters: {drain id: {key: {day: hash}}}.
DrainAnswer = dict[str, dict[str, dict[str, dict]]]


@dataclass
class Drain:
	"""One gateway's answer, sorted for landing and for the ack back to the box."""

	by_team: dict = field(default_factory=dict)  # {team: {drain id: {key: {day: hash}}}}
	acks: dict = field(default_factory=dict)  # {drain id: {key: [days]}}
	dead_acks: list = field(default_factory=list)
	pulled: int = 0
	stuck: int = 0

	def hold(self, team, drain_id, prefix, day, h):
		self.by_team.setdefault(team, {}).setdefault(drain_id, {}).setdefault(prefix, {})[day] = h

	def acknowledge(self, drain_id, prefix, days):
		self.acks.setdefault(drain_id, {}).setdefault(prefix, []).extend(days)
