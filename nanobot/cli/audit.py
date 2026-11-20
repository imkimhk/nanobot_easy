"""Read local action receipts without loading credentials or creating a database."""

import json
import sqlite3
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from nanobot.security.action_audit import ActionAuditStore


def create_audit_app():
    app = typer.Typer(help='View local action audit receipts; no execution or permission changes.')

    @app.callback()
    def audit():
        pass

    @app.command('list')
    def list_actions(
        workspace: Path = typer.Option(..., '--workspace', '-w', help='Exact gateway workspace'),
        limit: int = typer.Option(50, '--limit', min=1, max=500),
        session: str | None = typer.Option(None, '--session', help='Filter by original session key'),
        status: str | None = typer.Option(None, '--status'),
        as_json: bool = typer.Option(False, '--json'),
    ):
        try:
            rows = ActionAuditStore(workspace).read(limit=limit, session_key=session, status=status)
        except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
            typer.echo(f'Cannot read action audit ({type(exc).__name__}).', err=True)
            raise typer.Exit(1) from exc
        if as_json:
            typer.echo(json.dumps(rows, ensure_ascii=False, indent=2))
            return
        table = Table('Time (UTC)', 'Source', 'Kind', 'Tool', 'Status', 'Call reference')
        for row in rows:
            table.add_row(row['ts'], row['source'], row['kind'], row['tool'], row['status'],
                          (row['call_ref'] or row['subject_ref'] or '')[:12])
        Console().print(table)
        typer.echo(f'{len(rows)} receipt(s). tool_ok is not proof of external delivery or user approval.')

    return app
