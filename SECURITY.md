# Security policy

## Reporting a vulnerability

Please report security issues privately through GitHub's
[private vulnerability reporting](https://github.com/awsmankit/naap/security/advisories/new).
Don't open a public issue. Include the steps to reproduce and the version or commit. You should get
a reply within a week.

## Deployment notes

- Naap has **no authentication**. By default it binds to `127.0.0.1` only. If you set `HOST=0.0.0.0`
  (as the Docker image does), anyone who can reach the port can upload drawings and read every job.
  Put it behind your own authentication, or publish the port on localhost only (as
  `docker-compose.yml` does).
- Uploaded drawings and results are stored unencrypted under `DWG_MB_DATA` (default `data/jobs/`).
  Delete jobs you no longer need.
- Untrusted files are parsed by LibreDWG (`dwg2dxf`, run as a separate process with a timeout),
  ezdxf and MuPDF. Keep them up to date. Size limits: `DWG_MB_MAX_UPLOAD_MB`, `DWG_MB_MAX_PDF_MB`
  and `DWG_MB_MAX_PDF_PAGES`.
