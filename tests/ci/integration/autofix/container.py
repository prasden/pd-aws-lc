import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from strands.sandbox.docker import DockerSandbox

type Flags = list[str]
type Variables = dict[str, str]


class Access(StrEnum):
    READ_ONLY = "ro"
    READ_WRITE = "rw"


class Network(StrEnum):
    NONE = "none"
    BRIDGE = "bridge"


@dataclass(frozen=True)
class Mount:
    host: Path
    container: Path
    access: Access = Access.READ_WRITE

    def flag(self) -> str:
        return f"--volume={self.host}:{self.container}:{self.access}"


@dataclass(frozen=True)
class Container:
    name: str
    image: str
    workdir: Path
    mounts: list[Mount]
    network: Network = Network.NONE
    env: Variables = field(default_factory=dict)
    sysctls: Variables = field(default_factory=dict)
    user: str = ""
    locked_down: bool = False

    def flags(self) -> Flags:
        flags = [
            f"--name={self.name}",
            f"--workdir={self.workdir}",
            f"--network={self.network}",
            *(mount.flag() for mount in self.mounts),
            *(f"--env={key}={value}" for key, value in self.env.items()),
            *(f"--sysctl={key}={value}" for key, value in self.sysctls.items()),
        ]
        if self.user:
            flags.append(f"--user={self.user}")
        if self.locked_down:
            flags += ["--cap-drop=ALL", "--security-opt=no-new-privileges"]
        return flags

    # Start the container with only sleep running so commands exec into it, and remove it on exit.
    # Example: with Container(...).start() as sandbox: gives the agent a DockerSandbox bound to this container.
    @contextmanager
    def start(self) -> Iterator[DockerSandbox]:
        subprocess.run(["docker", "run", "--detach", "--rm", *self.flags(), self.image, "sleep", "infinity"],
                       check=True, capture_output=True)
        try:
            yield DockerSandbox(self.name)
        finally:
            subprocess.run(["docker", "rm", "--force", self.name], capture_output=True)
