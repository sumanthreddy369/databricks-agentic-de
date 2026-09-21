from agent.orchestrator import OrchestratorAgent


class ScriptedRouterClaude:
    """Fake Claude with the same run_tool_loop signature as agent.llm.Claude,
    scripted to return a fixed sequence of raw text responses. Makes zero
    real network/LLM calls.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def run_tool_loop(self, system, messages, tools, dispatch, max_turns=6):
        self.calls += 1
        return self._responses.pop(0)


def test_route_returns_de_then_da():
    fake = ScriptedRouterClaude(['{"mode":"de"}', '{"mode":"da"}'])
    orchestrator = OrchestratorAgent(claude=fake)

    assert orchestrator._route("is the pipeline healthy?") == "de"
    assert orchestrator._route("how many patients are in the ICU?") == "da"
    assert fake.calls == 2


def test_route_falls_back_to_da_on_malformed_json():
    fake = ScriptedRouterClaude(["this is not json at all"])
    orchestrator = OrchestratorAgent(claude=fake)

    assert orchestrator._route("anything") == "da"


def test_route_falls_back_to_da_on_unrecognized_mode_value():
    fake = ScriptedRouterClaude(['{"mode": "something_else"}'])
    orchestrator = OrchestratorAgent(claude=fake)

    assert orchestrator._route("anything") == "da"
