# Python Coding Guidelines

> Goose reads this file automatically at the start of every session run from this directory.
> Edit the rules below to match your preferences.

## Language & Environment
- Target language: Python 3.11+
- Always write code as standalone scripts unless asked otherwise.
- Prefer the standard library first; only add third-party packages if necessary and state why.

## Code Style
- Follow PEP 8 strictly.
- Use 4-space indentation, no tabs.
- Use `snake_case` for functions and variables, `PascalCase` for classes, `UPPER_SNAKE` for constants.
- Maximum line length: 100 characters.
- Add a module docstring at the top of every file explaining its purpose.

## Functions & Structure
- Keep functions small and focused (single responsibility).
- Add type hints to all function signatures.
- Add a short docstring to every function (Google style).
- Group related code; place a `main()` function and a `if __name__ == "__main__":` guard in runnable scripts.

## Error Handling
- Use specific exceptions, never a bare `except:`.
- Validate inputs early and fail fast with clear messages.

## Testing
- Write unit tests with `pytest` for any non-trivial logic.
- Place tests in a `tests/` directory, mirroring the source structure.

## Comments & Documentation
- Comment the "why", not the "what".
- Keep comments up to date with code changes.

## Workflow Rules for Goose
- Always run the code after writing it to confirm it works.
- Run `python -m py_compile <file>` to check syntax before finishing.
- Summarize the files created/changed and how to run them.

## Personal Conventions
- use argparse to expose parameters, help menu, verbose option, cProfile option
- write a log file when executing, called script.json
- standard script parameters should be in a file called parameters.json
- for loops over directories use a progress bar 

## Project-specific Conventions
<!-- Add anything unique to this repo here -->
