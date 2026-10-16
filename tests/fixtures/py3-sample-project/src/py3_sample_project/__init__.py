from httpx import get
from pydantic import BaseModel
from rich.console import Console


class Dep(BaseModel):
    name: str
    version: str


def main() -> None:
    console = Console()
    deps = [Dep(name="rich", version="ok"), Dep(name="httpx", version="ok")]

    for dep in deps:
        console.print(f"[green]{dep.name}[/] {dep.version} -> {get.__module__}")

    console.print(f"[bold cyan]resolved {len(deps) + 1} dependencies[/]")


if __name__ == "__main__":
    main()
