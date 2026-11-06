"""Provider clients: request, transfer and native-cell extraction for each data source.

Each module consumes a frozen request specification (locks/<source>.json) and
never filters, fills or rescales observations.
"""
