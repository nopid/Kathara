import logging
import shlex
from itertools import islice
from multiprocessing.dummy import Pool
from typing import List, Dict, Generator, Optional, Set, Tuple

import docker.models.containers
from docker import DockerClient
from docker.errors import APIError

from .DockerImage import DockerImage
from .stats.DockerMachineStats import DockerMachineStats
from ... import utils
from ...event.EventDispatcher import EventDispatcher
from ...exceptions import MountDeniedError, MachineAlreadyExistsError
from ...model.Lab import Lab
from ...model.Link import BRIDGE_LINK_NAME
from ...model.Machine import Machine
from ...setting.Setting import Setting

RP_FILTER_NAMESPACE = "net.ipv4.conf.%s.rp_filter"

# Known commands that each container should execute
# Run order: shared.startup, machine.startup and machine.startup_commands
STARTUP_COMMANDS = [
    # Unmount the /etc/resolv.conf and /etc/hosts files, automatically mounted by Docker inside the container.
    # In this way, they can be overwritten by custom user files.
    "umount /etc/resolv.conf",
    "umount /etc/hosts",

    # Copy the machine folder (if present) from the hostlab directory into the root folder of the container
    # In this way, files are all replaced in the container root folder
    "if [ -d \"/hostlab/{machine_name}\" ]; then "
    "(cd /hostlab/{machine_name} && tar c .) | (cd / && tar xhf -); fi",

    # If /etc/hosts is not configured by the user, add the localhost mapping
    "if [ ! -s \"/etc/hosts\" ]; then echo '127.0.0.1 localhost' > /etc/hosts; fi",

    # Give proper permissions to /var/www
    "if [ -d \"/var/www\" ]; then "
    "chmod -R 777 /var/www/*; fi",

    # Give proper permissions to Quagga files (if present)
    "if [ -d \"/etc/quagga\" ]; then "
    "chown quagga:quagga /etc/quagga/*",
    "chmod 640 /etc/quagga/*; fi",

    # Give proper permissions to FRR files (if present)
    "if [ -d \"/etc/frr\" ]; then "
    "chown frr:frr /etc/frr/*",
    "chmod 640 /etc/frr/*; fi",

    # If shared.startup file is present
    "if [ -f \"/hostlab/shared.startup\" ]; then "
    # Give execute permissions to the file and execute it
    # We redirect the output "&>" to a debugging file
    "chmod u+x /hostlab/shared.startup",
    # Adds a line to enable command output
    "sed -i \"1s;^;set -x\\n\\n;\" /hostlab/shared.startup",
    "/hostlab/shared.startup &> /var/log/shared.log; fi",

    # If .startup file is present
    "if [ -f \"/hostlab/{machine_name}.startup\" ]; then "
    # Give execute permissions to the file and execute it
    # We redirect the output "&>" to a debugging file
    "chmod u+x /hostlab/{machine_name}.startup",
    # Adds a line to enable command output
    "sed -i \"1s;^;set -x\\n\\n;\" /hostlab/{machine_name}.startup",
    "/hostlab/{machine_name}.startup &> /var/log/startup.log; fi",

    # Placeholder for user commands
    "{machine_commands}"
]

SHUTDOWN_COMMANDS = [
    # If machine.shutdown file is present
    "if [ -f \"/hostlab/{machine_name}.shutdown\" ]; then "
    # Give execute permissions to the file and execute it
    "chmod u+x /hostlab/{machine_name}.shutdown; /hostlab/{machine_name}.shutdown; fi",

    # If shared.shutdown file is present
    "if [ -f \"/hostlab/shared.shutdown\" ]; then "
    # Give execute permissions to the file and execute it
    "chmod u+x /hostlab/shared.shutdown; /hostlab/shared.shutdown; fi"
]


class DockerMachine(object):
    """The class responsible for deploying Kathara devices as Docker container and interact with them."""
    __slots__ = ['client', 'docker_image']

    def __init__(self, client: DockerClient, docker_image: DockerImage) -> None:
        self.client: DockerClient = client

        self.docker_image: DockerImage = docker_image

    def deploy_machines(self, lab: Lab, selected_machines: Set[str] = None) -> None:
        """Deploy all the lab devices as Docker containers.

        Args:
            lab (Kathara.model.Lab.Lab): A Kathara network scenario.
            selected_machines (Set[str]): A set containing the name of the devices to deploy.

        Returns:
            None
        """
        # Check and pulling machine images
        lab_images = set(map(lambda x: x.get_image(), lab.machines.values()))
        self.docker_image.check_and_pull_from_list(lab_images)

        shared_mount = lab.general_options['shared_mount'] if 'shared_mount' in lab.general_options \
            else Setting.get_instance().shared_mount

        if shared_mount:
            if Setting.get_instance().remote_url is not None:
                logging.warning("Shared folder cannot be mounted with a remote Docker connection.")
            else:
                lab.create_shared_folder()

        machines = {k: v for (k, v) in lab.machines.items() if k in selected_machines}.items() if selected_machines \
            else lab.machines.items()

        EventDispatcher.get_instance().dispatch("machines_deploy_started", items=machines)

        # Deploy all lab machines.
        # If there is no lab.dep file, machines can be deployed using multithreading.
        # If not, they're started sequentially
        if not lab.has_dependencies:
            pool_size = utils.get_pool_size()
            machines_pool = Pool(pool_size)

            items = utils.chunk_list(machines, pool_size)

            for chunk in items:
                machines_pool.map(func=self._deploy_and_start_machine, iterable=chunk)
        else:
            for item in machines:
                self._deploy_and_start_machine(item)

        EventDispatcher.get_instance().dispatch("machines_deploy_ended")

    def _deploy_and_start_machine(self, machine_item: Tuple[str, Machine]) -> None:
        """Deploy and start a Docker container from the device contained in machine_item.

        Args:
            machine_item (Tuple[str, Machine]): A tuple composed by the name of the device and a device object

        Returns:
            None
        """
        (_, machine) = machine_item

        self.create(machine)
        self.start(machine)

        EventDispatcher.get_instance().dispatch("machine_deployed", item=machine)

    def create(self, machine: Machine) -> None:
        """Create a Docker container representing the device and assign it to machine.api_object.

        Args:
            machine (Kathara.model.Machine.Machine): A Kathara device.

        Returns:
            None
        """
        logging.debug("Creating device `%s`..." % machine.name)

        containers = self.get_machines_api_objects_by_filters(machine_name=machine.name, lab_hash=machine.lab.hash,
                                                              user=utils.get_current_user_name())
        if containers:
            raise MachineAlreadyExistsError("Device with name `%s` already exists." % machine.name)

        image = machine.get_image()
        memory = machine.get_mem()
        cpus = machine.get_cpu(multiplier=1000000000)

        ports_info = machine.get_ports()
        ports = None
        if ports_info:
            ports = {}
            for (host_port, protocol), guest_port in ports_info.items():
                ports['%d/%s' % (guest_port, protocol)] = host_port

        # Get the general options into a local variable (just to avoid accessing the lab object every time)
        options = machine.lab.general_options

        # If bridged is required in command line but not defined in machine meta, add it.
        if "bridged" in options and not machine.meta['bridged']:
            machine.add_meta("bridged", True)

        if ports and not machine.meta['bridged']:
            logging.warning(
                "To expose ports of device `%s` on the host, "
                "you have to specify the `bridged` option on that device." % machine.name
            )

        # If any exec command is passed in command line, add it.
        if "exec" in options:
            machine.add_meta("exec", options["exec"])

        # Get the first network object, if defined.
        # This should be used in container create function
        first_network = None
        if machine.interfaces:
            first_network = machine.interfaces[0].api_object

        # If no interfaces are declared in machine, but bridged mode is required, get bridge as first link.
        # Flag that bridged is already connected (because there's another check in `start`).
        if first_network is None and machine.meta['bridged']:
            first_network = machine.lab.get_or_new_link(BRIDGE_LINK_NAME).api_object
            machine.add_meta("bridge_connected", True)

        # Sysctl params to pass to the container creation
        sysctl_parameters = {RP_FILTER_NAMESPACE % x: 0 for x in ["all", "default", "lo"]}

        if first_network:
            sysctl_parameters[RP_FILTER_NAMESPACE % "eth0"] = 0

        sysctl_parameters["net.ipv4.ip_forward"] = 1
        sysctl_parameters["net.ipv4.icmp_ratelimit"] = 0

        if machine.is_ipv6_enabled():
            sysctl_parameters["net.ipv6.conf.all.forwarding"] = 1
            sysctl_parameters["net.ipv6.icmp.ratelimit"] = 0
            sysctl_parameters["net.ipv6.conf.default.disable_ipv6"] = 0
            sysctl_parameters["net.ipv6.conf.all.disable_ipv6"] = 0

        # Merge machine sysctls
        sysctl_parameters = {**sysctl_parameters, **machine.meta['sysctls']}

        volumes = {}

        shared_mount = options['shared_mount'] if 'shared_mount' in options else Setting.get_instance().shared_mount
        if shared_mount and machine.lab.shared_folder:
            volumes[machine.lab.shared_folder] = {'bind': '/shared', 'mode': 'rw'}

        # Mount the host home only if specified in settings.
        hosthome_mount = options['hosthome_mount'] if 'hosthome_mount' in options else \
            Setting.get_instance().hosthome_mount
        if hosthome_mount and Setting.get_instance().remote_url is None:
            volumes[utils.get_current_user_home()] = {'bind': '/hosthome', 'mode': 'rw'}

        privileged = options['privileged_machines'] if 'privileged_machines' in options else False
        if Setting.get_instance().remote_url is not None and privileged:
            privileged = False
            logging.warning("Privileged flag is ignored with a remote Docker connection.")

        container_name = self.get_container_name(machine.name, machine.lab.hash)

        try:
            machine_container = self.client.containers.create(image=image,
                                                              name=container_name,
                                                              hostname=machine.name,
                                                              cap_add=machine.capabilities if not privileged else None,
                                                              privileged=privileged,
                                                              network=first_network.name if first_network else None,
                                                              network_mode="bridge" if first_network else "none",
                                                              environment=machine.meta['envs'],
                                                              sysctls=sysctl_parameters,
                                                              mem_limit=memory,
                                                              nano_cpus=cpus,
                                                              ports=ports,
                                                              tty=True,
                                                              stdin_open=True,
                                                              detach=True,
                                                              volumes=volumes,
                                                              labels={"name": machine.name,
                                                                      "lab_hash": machine.lab.hash,
                                                                      "user": utils.get_current_user_name(),
                                                                      "app": "kathara",
                                                                      "shell": machine.meta["shell"]
                                                                      if "shell" in machine.meta
                                                                      else Setting.get_instance().device_shell
                                                                      }
                                                              )
        except APIError as e:
            raise e

        # Pack machine files into a tar.gz and extract its content inside `/`
        tar_data = machine.pack_data()
        if tar_data:
            self.copy_files(machine_container, "/", tar_data)

        machine.api_object = machine_container

    def update(self, machine: Machine) -> None:
        """Update the Docker container representing the machine.

        Create a new Docker network for each collision domain contained in machine.interfaces that is not already
        attached to the container.

        Args:
            machine (Kathara.model.Machine.Machine): A Kathara device.

        Returns:
            None
        """
        containers = self.get_machines_api_objects_by_filters(
            machine_name=machine.name, lab_hash=machine.lab.hash, user=utils.get_current_user_name()
        )

        if not containers:
            raise Exception("The specified device `%s` is not running." % machine.name)

        machine.api_object = containers.pop()
        attached_networks = machine.api_object.attrs["NetworkSettings"]["Networks"]

        # Connect the container to its new networks
        for (_, machine_link) in machine.interfaces.items():
            if machine_link.api_object.name not in attached_networks:
                machine_link.api_object.connect(machine.api_object)

    @staticmethod
    def start(machine: Machine) -> None:
        """Start the Docker container representing the device.

        Connect the container to the networks, run the startup commands and open a terminal (if requested).

        Args:
           machine (Kathara.model.Machine.Machine): A Kathara device.
        """
        logging.debug("Starting device `%s`..." % machine.name)

        try:
            machine.api_object.start()
        except APIError as e:
            if e.response.status_code == 500 and e.explanation.startswith('Mounts denied'):
                raise MountDeniedError("Host drive is not shared with Docker.")
            else:
                raise e

        # Connect the container to its networks (starting from the second, the first is already connected in `create`)
        # This should be done after the container start because Docker causes a non-deterministic order when attaching
        # networks before container startup.
        for (iface_num, machine_link) in islice(machine.interfaces.items(), 1, None):
            logging.debug("Connecting device `%s` to collision domain `%s` on interface %d..." % (machine.name,
                                                                                                  machine_link.name,
                                                                                                  iface_num
                                                                                                  )
                          )

            machine_link.api_object.connect(machine.api_object)

        # Bridged connection required but not added in `deploy` method.
        if "bridge_connected" not in machine.meta and machine.meta['bridged']:
            bridge_link = machine.lab.get_or_new_link(BRIDGE_LINK_NAME).api_object
            bridge_link.connect(machine.api_object)

        # Append executed machine startup commands inside the /var/log/startup.log file
        if machine.startup_commands:
            new_commands = []
            for command in machine.startup_commands:
                new_commands.append("echo \"++ %s\" &>> /var/log/startup.log" % command)
                new_commands.append(command)
            machine.startup_commands = new_commands

        # Build the final startup commands string
        startup_commands_string = "; ".join(STARTUP_COMMANDS).format(
            machine_name=machine.name,
            machine_commands="; ".join(machine.startup_commands)
        )

        # Execute the startup commands inside the container (without privileged flag so basic permissions are used)
        machine.api_object.exec_run(cmd=[Setting.get_instance().device_shell, '-c', startup_commands_string],
                                    stdout=False,
                                    stderr=False,
                                    privileged=False,
                                    detach=True
                                    )

    def undeploy(self, lab_hash: str, selected_machines: Set[str] = None) -> None:
        """Undeploy the devices contained in the network scenario defined by the lab_hash.

        If a set of selected_machines is specified, undeploy only the specified devices.

        Args:
            lab_hash (str): The hash of the network scenario to undeploy.
            selected_machines (Optional[Set[str]]): If not None, undeploy only the specified devices.

        Returns:
            None
        """
        containers = self.get_machines_api_objects_by_filters(lab_hash=lab_hash, user=utils.get_current_user_name())

        if selected_machines is not None and len(selected_machines) > 0:
            containers = [item for item in containers if item.labels["name"] in selected_machines]

        if len(containers) > 0:
            pool_size = utils.get_pool_size()
            machines_pool = Pool(pool_size)

            items = utils.chunk_list(containers, pool_size)

            EventDispatcher.get_instance().dispatch("machines_undeploy_started", items=containers)

            for chunk in items:
                machines_pool.map(func=self._undeploy_machine, iterable=chunk)

            EventDispatcher.get_instance().dispatch("machines_undeploy_ended")

    def wipe(self, user: str = None) -> None:
        """Undeploy all the running devices of the specified user. If user is None, it undeploy all the running devices.

        Args:
            user (str): The name of a current user on the host.

        Returns:
            None
        """
        containers = self.get_machines_api_objects_by_filters(user=user)

        pool_size = utils.get_pool_size()
        machines_pool = Pool(pool_size)

        items = utils.chunk_list(containers, pool_size)

        for chunk in items:
            machines_pool.map(func=self._undeploy_machine, iterable=chunk)

    def _undeploy_machine(self, machine_api_object: docker.models.containers.Container) -> None:
        """Undeploy a Docker container.

        Args:
            machine_api_object (docker.models.containers.Container): The Docker container to undeploy.

        Returns:
            None
        """
        self._delete_machine(machine_api_object)

        EventDispatcher.get_instance().dispatch("machine_undeployed", item=machine_api_object)

    def connect(self, lab_hash: str, machine_name: str, user: str = None, shell: str = None,
                logs: bool = False) -> None:
        """Open a stream to the Docker container specified by machine_name using the specified shell.

        Args:
            lab_hash (str): The hash of the network scenario containing the device.
            machine_name (str): The name of the device to connect.
            user (str): The name of a current user on the host.
            shell (str): The path to the desired shell.
            logs (bool): If True, print the logs of the startup command.

        Returns:
            None
        """
        containers = self.get_machines_api_objects_by_filters(lab_hash=lab_hash, machine_name=machine_name, user=user)
        if not containers:
            raise Exception("The specified device `%s` is not running." % machine_name)
        container = containers.pop()

        if not shell:
            shell = shlex.split(container.labels['shell'])
        else:
            shell = shlex.split(shell) if type(shell) == str else shell

        logging.debug("Connect to device `%s` with shell: %s" % (machine_name, shell))

        if logs and Setting.get_instance().print_startup_log:
            exec_output = self.exec(lab_hash,
                                    machine_name,
                                    user=user,
                                    command="cat /var/log/shared.log /var/log/startup.log",
                                    tty=False
                                    )

            startup_output = ""
            try:
                while True:
                    (stdout, _) = next(exec_output)
                    startup_output += stdout.decode('utf-8') if stdout else ""
            except StopIteration:
                pass

            if startup_output:
                print("--- Startup Commands Log\n")
                print(startup_output)
                print("--- End Startup Commands Log\n")

        resp = self.client.api.exec_create(container.id,
                                           shell,
                                           stdout=True,
                                           stderr=True,
                                           stdin=True,
                                           tty=True,
                                           privileged=False
                                           )

        exec_output = self.client.api.exec_start(resp['Id'],
                                                 tty=True,
                                                 socket=True
                                                 )

        def tty_connect():
            from .terminal.DockerTTYTerminal import DockerTTYTerminal
            DockerTTYTerminal(exec_output, self.client, resp['Id']).start()

        def cmd_connect():
            from .terminal.DockerNPipeTerminal import DockerNPipeTerminal
            DockerNPipeTerminal(exec_output, self.client, resp['Id']).start()

        utils.exec_by_platform(tty_connect, cmd_connect, tty_connect)

    def exec(self, lab_hash: str, machine_name: str, command: str, user: str = None,
             tty: bool = True) -> Generator[Tuple[bytes, bytes], None, None]:
        """Execute the command on the Docker container specified by the lab_hash and the machine_name.

        Args:
            lab_hash (str): The hash of the network scenario containing the device.
            machine_name (str): The name of the device.
            user (str): The name of a current user on the host.
            command (str): The command to execute.
            tty (bool): If True, open a new tty.

        Returns:
            Generator[Tuple[bytes, bytes]]: A generator of tuples containing the stdout and stderr in bytes.
        """
        logging.debug("Executing command `%s` to device with name: %s" % (command, machine_name))

        containers = self.get_machines_api_objects_by_filters(lab_hash=lab_hash, machine_name=machine_name, user=user)
        if not containers:
            raise Exception("The specified device `%s` is not running." % machine_name)
        container = containers.pop()

        exec_result = container.exec_run(cmd=command,
                                         stdout=True,
                                         stderr=True,
                                         tty=tty,
                                         privileged=False,
                                         stream=True,
                                         demux=True,
                                         detach=False
                                         )

        return exec_result.output

    @staticmethod
    def copy_files(machine_api_object: docker.models.containers.Container, path: str, tar_data: bytes) -> None:
        """Copy the files contained in tar_data in the Docker container path specified by the machine_api_object.

        Args:
            machine_api_object (docker.models.containers.Container): A Docker container.
            path (str): The path of the container where copy the tar_data.
            tar_data (bytes): The data to copy in the container.

        Returns:
            None
        """
        machine_api_object.put_archive(path, tar_data)

    def get_machines_api_objects_by_filters(self, lab_hash: str = None, machine_name: str = None, user: str = None) -> \
            List[docker.models.containers.Container]:
        """Return the Docker containers objects specified by lab_hash and user.

        Args:
            lab_hash (str): The hash of a network scenario. If specified, return all the devices in the scenario.
            machine_name (str): The name of a device. If specified, return the specified container of the scenario.
            user (str): The name of a user on the host. If specified, return only the containers of the user.

        Returns:
            List[docker.models.containers.Container]: A list of Docker containers objects.
        """
        filters = {"label": ["app=kathara"]}
        if user:
            filters["label"].append("user=%s" % user)
        if lab_hash:
            filters["label"].append("lab_hash=%s" % lab_hash)
        if machine_name:
            filters["label"].append("name=%s" % machine_name)

        return self.client.containers.list(all=True, filters=filters)

    def get_machines_stats(self, lab_hash: str = None, machine_name: str = None, user: str = None) -> \
            Generator[Dict[str, DockerMachineStats], None, None]:
        """Return a generator containing the Docker devices' stats.

        Args:
            lab_hash (str): The hash of a network scenario. If specified, return all the stats of the devices in the
                scenario.
            machine_name (str): The name of a device. If specified, return the specified device stats.
            user (str): The name of a user on the host. If specified, return only the stats of the specified user.

        Returns:
            Generator[Dict[str, DockerMachineStats], None, None]: A generator containing device names as keys and
            DockerMachineStats as values.
        """
        containers = self.get_machines_api_objects_by_filters(lab_hash=lab_hash, machine_name=machine_name, user=user)
        if not containers:
            if not machine_name:
                raise Exception("No devices found.")
            else:
                raise Exception(f"Devices with name {machine_name} not found.")

        containers = sorted(containers, key=lambda x: x.name)

        machine_streams = {}

        for machine in containers:
            machine_streams[machine.name] = DockerMachineStats(machine)

        while True:
            for machine_stats in machine_streams.values():
                try:
                    machine_stats.update()
                except StopIteration:
                    continue

            yield machine_streams

    @staticmethod
    def get_container_name(name: str, lab_hash: str) -> str:
        """Return the name of a Docker container.

        Args:
            name (str): The name of a Kathara device.
            lab_hash (str): The hash of a running scenario.

        Returns:
            str: The name of the Docker container in the format "|dev_prefix|_|username_prefix|_|name|_|lab_hash|".
        """
        lab_hash = lab_hash if "_%s" % lab_hash else ""
        return "%s_%s_%s_%s" % (Setting.get_instance().device_prefix, utils.get_current_user_name(), name, lab_hash)

    @staticmethod
    def _delete_machine(machine: docker.models.containers.Container) -> None:
        """Remove a running Docker container.

        Args:
            machine (docker.models.containers.Container): The Docker container to remove.

        Returns:
            None
        """
        # Build the shutdown command string
        shutdown_commands_string = "; ".join(SHUTDOWN_COMMANDS).format(machine_name=machine.labels["name"])

        # Execute the shutdown commands inside the container (only if it's running)
        if machine.status == "running":
            machine.exec_run(cmd=[Setting.get_instance().device_shell, '-c', shutdown_commands_string],
                             stdout=False,
                             stderr=False,
                             privileged=True,
                             detach=True
                             )

        machine.remove(force=True)
