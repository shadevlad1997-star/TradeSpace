# Third-party assets

TradeSpace product code is private. The licenses of third-party components are
not replaced by the product identity or the new repository history.

## Locally distributed Swagger UI

- Component: Swagger UI 5.17.14 (`swagger-ui-dist`), SmartBear Software Inc.
- Official source: https://github.com/swagger-api/swagger-ui/tree/v5.17.14
- Official distribution: https://registry.npmjs.org/swagger-ui-dist/5.17.14
- Assets: `app/static/swagger-ui-bundle.js` and `app/static/swagger-ui.css`.
- The assets match that exact official distribution after CRLF normalization;
  no JavaScript or CSS was replaced for publication.
- Apache-2.0 text: `app/static/swagger-ui.LICENSE.txt`.
- Original publisher NOTICE: `app/static/swagger-ui.NOTICE.txt`.
- The upstream npm distribution omits the webpack companion referenced by the
  bundle header. `swagger-ui-bundle.js.LICENSE.txt` explicitly reproduces its
  supplied LICENSE and NOTICE at that path; it is not claimed to be a recovered
  upstream webpack-generated license inventory.
- Embedded original attribution comments remain intact.

The CSS includes normalize.css v7.0.0 (MIT). Its original license from the exact
npm package is retained at `app/static/normalize.LICENSE.txt`.

Python dependencies and their transitive licenses remain in their installed
wheel/package metadata. The pinned application dependencies are listed in
`pyproject.toml` and `requirements.lock`; installation must retain distribution
license files. This repository does not relicense those dependencies.

The release allowlist and Docker context include these legal text files. No
runtime secrets or downloaded package archives are shipped with them.
