from typing import Optional, Dict, Any

from ...foundation.setting.SettingsAddon import SettingsAddon

DEFAULTS = {
    "hosthome_mount": False,
    "shared_mount": True,
    "image_update_policy": "Prompt",
    "shared_cd": False,
    "remote_url": None,
    "wire_ports": "4305-4319",
    "wire_command": "wireshark",
    "wire_image": "nopid/wire:latest",
    "wire_snoop": "kathara_snoop",
    "cert_path": None
}


class DockerSettingsAddon(SettingsAddon):
    __slots__ = ['hosthome_mount', 'shared_mount', 'image_update_policy', 'shared_cd', 'remote_url', 'wire_ports', 'wire_command', 'wire_image', 'wire_snoop', 'cert_path']

    def __init__(self) -> None:
        self.hosthome_mount: bool = False
        self.shared_mount: bool = True
        self.image_update_policy: str = 'Prompt'
        self.shared_cd: bool = False
        self.remote_url: Optional[str] = None
        self.wire_ports: Optional[str] = "4305-4319"
        self.wire_command: Optional[str] = "wireshark"
        self.wire_image: Optional[str] = "nopid/wire:latest"
        self.wire_snoop: Optional[str] = "kathara_snoop"
        self.cert_path: Optional[str] = None

    def _to_dict(self) -> Dict[str, Any]:
        return {
            'hosthome_mount': self.hosthome_mount,
            'shared_mount': self.shared_mount,
            'image_update_policy': self.image_update_policy,
            'shared_cd': self.shared_cd,
            'remote_url': self.remote_url,
            'wire_ports': self.wire_ports,
            'wire_command': self.wire_command,
            'wire_image': self.wire_image,
            'wire_snoop': self.wire_snoop,
            'cert_path': self.cert_path
        }
