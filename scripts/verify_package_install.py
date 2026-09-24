from importlib.resources import files


def main() -> int:
    import app
    import scripts

    del app, scripts
    app_files = files('app')
    required_data = (
        app_files / 'templates' / 'cabinet.html',
        app_files / 'static' / 'tradespace/brand-mark.svg',
    )
    missing = [str(path) for path in required_data if not path.is_file()]
    if missing:
        raise RuntimeError(
            'installed application package is missing required package data'
        )
    print('application_package_verification=OK')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
