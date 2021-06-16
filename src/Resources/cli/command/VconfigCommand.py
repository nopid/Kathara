import argparse
import logging
import re
import sys

from ...foundation.cli.command.Command import Command
from ...manager.Kathara import Kathara
from ...model.Lab import Lab
from ...strings import strings, wiki_description


class VconfigCommand(Command):
    def __init__(self):
        Command.__init__(self)

        parser = argparse.ArgumentParser(
            prog='kathara vconfig',
            description=strings['vconfig'],
            epilog=wiki_description,
            add_help=False
        )

        parser.add_argument(
            '-h', '--help',
            action='help',
            default=argparse.SUPPRESS,
            help='Show an help message and exit.'
        )
        parser.add_argument(
            '-n', '--name',
            metavar='DEVICE_NAME',
            required=True,
            help='Name of the device to be connected on desired collision domains.'
        )
        parser.add_argument(
            '--eth',
            dest='eths',
            metavar='CD',
            nargs='+',
            required=True,
            help='Specify the collision domain for an interface.'
        )

        self.parser = parser

    def run(self, current_path, argv):
        self.parse_args(argv)
        args = self.get_args()

        for eth in args['eths']:
            # Only alphanumeric characters are allowed
            matches = re.search(r"^\w+$", eth)

            if not matches:
                sys.stderr.write('Syntax error in --eth field.\n')
                self.parser.print_help()
                exit(1)

        lab = Lab("kathara_vlab")

        device = lab.get_or_new_machine(args['name'])
        device.api_object = Kathara.get_instance().get_machine_api_object(lab.folder_hash, args['name'])

        for eth in args['eths']:
            logging.info("Adding interface to device `%s` for collision domain `%s`..." % (args['name'], eth))
            lab.connect_machine_to_link(args['name'], eth)

        Kathara.get_instance().update_lab(lab)
