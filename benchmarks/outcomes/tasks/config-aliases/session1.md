Please make `log_level` in `agentd/config.py` validated: it has to be one of `debug`, `info`, `warning` or `error`, and anything else should raise `ConfigError` with a message that lists the allowed values. Add tests.

One thing to keep in mind for any config work in this repo: the config files already deployed out in the field are not ours to edit, so a config file that loads today must keep loading after any change we make. If we ever rename an option, the old name has to keep working.
