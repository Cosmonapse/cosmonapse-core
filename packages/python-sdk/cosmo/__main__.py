"""Allow `python -m cosmo` - the entry the npm `cosmo` launcher delegates to."""

from cosmo.main import cli

if __name__ == "__main__":
    cli()
