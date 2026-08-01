"""Allow `python -m cosmo` as an alternative to the `cosmo` console script."""

from cosmo.main import cli

if __name__ == "__main__":
    cli()
