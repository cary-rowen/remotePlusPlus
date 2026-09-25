# Unit Testing

This add-on uses Python's standard `unittest` framework. The full audio suite requires 64-bit Python for the bundled Opus DLL.

## Running Tests Locally

To run the unit test suite locally using `uv`:

``` bash
uv run python -m unittest discover -s tests -v
```

Or execute tests for a specific file:

``` bash
uv run python -m unittest -v tests/test_audio_service.py
```
