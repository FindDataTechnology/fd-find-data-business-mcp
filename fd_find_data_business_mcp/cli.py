import click

from . import __version__
from .server import main


@click.group()
@click.version_option(__version__)
def cli():
    """fd-find-data-business-mcp — commercial FindData MCP server."""


@cli.command()
@click.option("--transport", type=click.Choice(["stdio", "http"]), default="stdio")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8310, show_default=True, type=int)
def serve(transport, host, port):
    """Serve the MCP server."""
    main(transport=transport, host=host, port=port)


if __name__ == "__main__":
    cli()
