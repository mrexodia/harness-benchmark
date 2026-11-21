# Optional hook, sourced in the agent container (as the agent user) just before
# the harness named in the file name starts (here: pi). Use it to install a
# provider extension, write a config file, or export extra env vars. Files in
# this folder are available read-only under /opt/provider/.
# Pi reads ANTHROPIC_API_KEY itself, so this example needs no setup.
:
