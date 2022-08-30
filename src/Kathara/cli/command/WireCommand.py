import argparse
import logging
import shutil
import sys
from typing import List

from ... import utils
from ... import wire
from ...foundation.cli.command.Command import Command
from ...manager.Kathara import Kathara
from ...setting.Setting import Setting
from ...strings import strings, wiki_description


class WireCommand(Command):
    def __init__(self) -> None:
        Command.__init__(self)

        self.parser: argparse.ArgumentParser = argparse.ArgumentParser(
            prog='kathara wire',
            description=strings['wire'],
            epilog=wiki_description,
            add_help=False,
        )

        self.parser.add_argument(
            '-h', '--help',
            action='help',
            default=argparse.SUPPRESS,
            help='Show an help message and exit.',
        )

        self.parser.add_argument(
            '-s', '--stop',
            required=False,
            action='store_true',
            help='Stop the wire VM.',
        )

        self.parser.add_argument(
            'cd',
            metavar='CD',
            nargs='*',
            help='Collision domain to capture packets from.',
        )

    def run(self, current_path: str, argv: List[str]) -> None:
        self.parse_args(argv)
        args = self.get_args()

        if Setting.get_instance().manager_type != "docker":
            logging.error("wire command requires docker manager")
            sys.exit(1)
        manager = Kathara.get_instance().manager
        docker_link = manager.docker_link
        if args['stop']:
            wire.stop_snoop()
        if len(args['cd']) > 0:
            wire.start_snoop()
            nets = [docker_link.get_network_name(cd) for cd in args['cd']]
            wire.capture(nets)
