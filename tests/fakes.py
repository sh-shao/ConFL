"""Deterministic stand-ins for the LLM, for tests only."""


class FirstOptionModel:
    """Selects the first current option at every stage; never calls tools."""
    def __init__(self):
        self.calls = []
        self.model = 'first-option'

    def begin(self):
        pass

    def __call__(self, request):
        return dict(name=request['options'][0]['name'], explanation='test model: first option')
