# AI-readiness reflection

## What did you learn from this exercise?

We learned that there are many valid ways to split a dataset which come with different tradeoffs. Weighting by stations gives each location equal importance, while weighting by sensor-days balances observation volume but gives more influence to stations with more sensors or longer records. For our ISMN dataset, we chose spatial folds balanced by unique station IDs to emphasize transfer to new locations.

## What did you learn from interacting with agents or chatbots?

We learned that agents can make subtle mistakes even when their scripts appear to work. We asked Codex to write the data-acquisition scripts, then asked Claude to review them; this caught a bug in which the MODIS land-cover code stored categorical classes as floats, with small numerical errors causing valid records to be misidentified as missing.

## How could the assignment better meet its learning goals?

Allow flexible repository structures rather than requiring a fixed directory layout. We store raw and processed data in Kopah object storage, so our data organization doesn't match the assignment's intended local repository structure.
