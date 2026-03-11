"""CLI entrypoint for faceit-finder."""

from __future__ import annotations

import sys
from datetime import UTC, datetime

import click
from rich import box
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from .api import FaceitClient
from .search import search_matches

console = Console()


def _ts(unix: int) -> str:
    if not unix:
        return "unknown"
    dt = datetime.fromtimestamp(unix, tz=UTC)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


@click.command()
@click.option(
    "--api-key",
    envvar="FACEIT_API_KEY",
    required=True,
    help="FACEIT API key (or set FACEIT_API_KEY env var).",
)
@click.option("--you", required=True, help="Your FACEIT nickname.")
@click.option("--opponent", required=True, help="Opponent's FACEIT nickname.")
@click.option(
    "--game",
    default="cs2",
    show_default=True,
    help="Game slug (cs2, csgo, valorant, …).",
)
@click.option(
    "--max-pages",
    default=50,
    show_default=True,
    help="Max pages of history to scan (100 matches/page).",
)
@click.option(
    "--show-teams/--no-show-teams",
    default=True,
    help="Show full team rosters in results.",
)
def main(
    api_key: str,
    you: str,
    opponent: str,
    game: str,
    max_pages: int,
    show_teams: bool,
) -> None:
    """Find FACEIT matches where YOU played against (or with) OPPONENT."""

    scanned_count = 0
    found_count = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
        console=console,
    ) as progress:
        task = progress.add_task("Scanning match history…", total=None)

        def on_progress(scanned: int, found: int) -> None:
            nonlocal scanned_count, found_count
            scanned_count = scanned
            found_count = found
            progress.update(
                task,
                description=f"Scanned [bold]{scanned}[/] matches, found [green]{found}[/] so far…",
            )

        try:
            with FaceitClient(api_key) as client:
                console.print(f"\n🔍 Looking for matches: [cyan]{you}[/] vs [yellow]{opponent}[/] in [bold]{game}[/]\n")
                matches = search_matches(
                    client,
                    your_nickname=you,
                    opponent_nickname=opponent,
                    game=game,
                    max_pages=max_pages,
                    progress_callback=on_progress,
                )
        except Exception as exc:
            console.print(f"[red]Error:[/] {exc}")
            sys.exit(1)

    console.print(
        f"✅ Scanned [bold]{scanned_count}[/] matches total. "
        f"Found [green bold]{len(matches)}[/] match(es) with [yellow]{opponent}[/].\n"
    )

    if not matches:
        console.print("[dim]No matches found. Try increasing --max-pages.[/]")
        return

    for i, m in enumerate(matches, 1):
        same_team = m.your_team == m.opponent_team

        table = Table(
            title=f"Match {i} of {len(matches)}  •  {_ts(m.started_at)}",
            box=box.ROUNDED,
            show_header=True,
            header_style="bold magenta",
            expand=False,
        )
        table.add_column("Field", style="dim", width=16)
        table.add_column("Value")

        table.add_row("Match ID", m.match_id)
        table.add_row("Date", _ts(m.started_at))
        table.add_row("Game Mode", m.game_mode)
        table.add_row("Map", m.map)
        table.add_row("Status", m.status)
        table.add_row("Score (your team)", m.score)
        table.add_row(
            "Same team?",
            "[green]Yes — teammates[/]" if same_team else "[red]No — opponents[/]",
        )
        table.add_row("URL", f"[link={m.faceit_url}]{m.faceit_url}[/link]")

        if show_teams:
            for team_name, roster in m.teams.items():
                label = team_name
                if team_name == m.your_team:
                    label += " [cyan](you)[/]"
                if team_name == m.opponent_team:
                    label += " [yellow](opp)[/]"
                roster_str = ", ".join(
                    f"[cyan]{n}[/]" if n == you else f"[yellow]{n}[/]" if n == opponent else n for n in roster
                )
                table.add_row(f"Team: {label}", roster_str)

        console.print(table)
        console.print()
