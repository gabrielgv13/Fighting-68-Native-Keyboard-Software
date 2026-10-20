import unittest

from fighting68.__main__ import build_parser, cmd_gui


class DefaultCommandTest(unittest.TestCase):
    def test_running_without_a_subcommand_defaults_to_the_real_device_gui(self):
        args = build_parser().parse_args([])
        self.assertIs(args.func, cmd_gui)


if __name__ == "__main__":
    unittest.main()
