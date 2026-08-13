# Contributing to CiteVerify

Thank you for helping improve CiteVerify.

## Before opening an issue

- Confirm that you are using the latest version.
- Include the operating system and Python version.
- Describe whether the problem occurred during PDF/Word parsing or during
  online verification.
- Never upload API keys, private papers, private diagnostics, or reports that
  contain sensitive information.

## Development setup

```powershell
python -m pip install -r requirements.txt
python .\citeverify_web.py --no-browser
```

Please test both the parser and the HTML interface after making changes.
