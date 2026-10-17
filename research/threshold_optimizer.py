"""Offline threshold optimizer interfaces; implemented in the next commit."""


def score_alerts(*args, **kwargs): return {}
def make_alerts(*args, **kwargs): return []
def clock_eligible(*args, **kwargs): return False
def split_groups(*args, **kwargs): return set(), set()
def choose_threshold(*args, **kwargs): return None
def pareto_frontier(*args, **kwargs): return []
def threshold_grid(*args, **kwargs): return []
def trade_features(*args, **kwargs): return []
def recommendation_blockers(*args, **kwargs): return []
