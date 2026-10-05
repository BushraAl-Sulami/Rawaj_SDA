"""Strategy generation must handle native tool calls and Responses text blocks."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableLambda

from agents.strategy_agent import strategy_agent as strategy


class StrategyExecutionTests(unittest.TestCase):
    def tearDown(self):
        strategy.get_executor.cache_clear()

    def test_calendar_tool_result_reaches_model_and_final_json_is_read(self):
        calls = []

        def reply(prompt):
            messages = prompt.to_messages()
            calls.append(messages)
            if len(calls) == 1:
                return AIMessage(content='', tool_calls=[{
                    'name': 'get_upcoming_events',
                    'args': {'start_date': '2026-09-27'}, 'id': 'calendar-call',
                }])
            self.assertIsInstance(messages[-1], ToolMessage)
            self.assertEqual(messages[-1].tool_call_id, 'calendar-call')
            return AIMessage(content=[{'type': 'text', 'text': '{"restaurant":"Cafe"}'}])

        model = SimpleNamespace(bind_tools=lambda tools: RunnableLambda(reply))
        with patch.dict(os.environ, {'LANGSMITH_TRACING': 'false', 'LANGCHAIN_TRACING_V2': 'false'}), \
                patch.object(strategy, 'get_llm', return_value=model):
            strategy.get_executor.cache_clear()
            result = strategy.get_executor().invoke({'input': 'Plan starting 2026-09-27'})
        self.assertEqual(strategy._strategy_json(result['output'], 'generation'), {'restaurant': 'Cafe'})
        self.assertEqual(len(calls), 2)

    def test_stopped_agent_never_enters_reflection(self):
        executor = Mock()
        executor.invoke.return_value = {'output': 'Agent stopped due to iteration limit or time limit.'}
        with patch.object(strategy, 'get_executor', return_value=executor), \
                patch.object(strategy, 'reflect_strategy') as reflect:
            with self.assertRaisesRegex(RuntimeError, 'limit before producing'):
                strategy.generate_strategy({}, '2026-09-27')
        reflect.assert_not_called()

    def test_invalid_outputs_are_rejected(self):
        for output in ('not json', '[]', '', None):
            with self.subTest(output=output), self.assertRaises(ValueError):
                strategy._strategy_json(output, 'generation')

    def test_fenced_json_and_structured_reflection(self):
        self.assertEqual(strategy._strategy_json('```json\n{"ok":true}\n```', 'generation'), {'ok': True})
        self.assertEqual(strategy._strategy_json('Final Answer: {"ok":true}', 'generation'), {'ok': True})
        model = Mock()
        corrected = {'restaurant': 'Cafe', 'primary_marketing_gaps': [],
                     'thirty_day_target': ['Improve profile clarity'],
                     'recommended_services': [], 'thirty_day_plan': [],
                     'external_trend_support': []}
        model.with_structured_output.return_value.invoke.return_value = strategy.StrategyReflection(
            passed=True, issues=[], corrected_strategy=corrected,
        )
        with patch.object(strategy, 'get_llm', return_value=model):
            result = strategy.reflect_strategy({}, {}, '2026-09-27')
        self.assertEqual(result['corrected_strategy'], corrected)
        model.with_structured_output.assert_called_once_with(
            strategy.StrategyReflection, method='json_schema', strict=True,
        )


if __name__ == '__main__':
    unittest.main()
