import unittest

from commands_lfm3 import parse_calls
from commands_runtime import CommandModelError
import commands_selection


class LFM3Tests(unittest.TestCase):
    def test_calls_preserve_literal_arguments(self):
        text = ('<tool_call>\n<function=message>\n'
                '<parameter=text>\n  hello & <world>  \n</parameter>\n'
                '</function>\n</tool_call>')
        self.assertEqual(parse_calls(text), [{'name': 'message', 'arguments': {'text': '  hello & <world>  '}}])

    def test_multiple_calls_and_reasoning(self):
        call = '<tool_call><function=wait><parameter=seconds>2</parameter></function></tool_call>'
        self.assertEqual(len(parse_calls('<think>consider</think>' + call * 2)), 2)

    def test_complete_refusal_is_abstention(self):
        self.assertEqual(parse_calls('No action is requested.<|im_end|>'), [])

    def test_malformed_outputs_fail_closed(self):
        for text in ('<tool_call>', '<think>unfinished',
                     '<tool_call><function=wait></function></tool_call>trailing',
                     '<tool_call><function=wait><parameter=x>1</parameter><parameter=x>2</parameter></function></tool_call>',
                     '<tool_call><function=wait></function></tool_call>' * 5,
                     'prose <tool_call><function=wait></function></tool_call>'):
            with self.subTest(text=text), self.assertRaises(CommandModelError):
                parse_calls(text)

    def test_explicit_selection_keeps_existing_default(self):
        self.assertEqual(commands_selection.resolve('lfm3'), 'lfm3')
        self.assertEqual(commands_selection.DEFAULT, 'lfm2')
        self.assertEqual(commands_selection.ASSETS['lfm3'], 'lfm3-kilix-commands')


if __name__ == '__main__':
    unittest.main()
