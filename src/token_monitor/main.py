import typer
import asyncio
from pathlib import Path
from typing import Optional
from rich.console import Console
from token_monitor.simulator import ContextSimulator

app = typer.Typer(help="Token Monitor: Optimize your Cursor context headroom.")
console = Console()

@app.command()
def simulate(
    config: Path = typer.Argument(..., help="Path to the proposed configuration file (JSON)."),
    omlx_endpoint: str = typer.Option("http://localhost:8080/v1/chat/completions", "--endpoint", "-e")
):
    """
    Predict the context expansion of a new configuration using OMLX.
    """
    if not config.exists():
        console.print(f"[red]Error: Configuration file {config} not found.[/red]")
        raise typer.Exit(1)

    async def run_simulation():
        # 1. Load and pre-process config (Simulating metadata extraction)
        try:
            with open(config, 'r') as f:
                raw_config = f.read()
            
            # In a real scenario, we'd convert this to the compact metadata schema
            # For this demo, we pass the raw content as a dict proxy
            mock_data = {
                "mcp_servers": [{"name": "test-server", "tool_count": 10}],
                "rules": [{"description": "Test rule", "estimated_token_count": 500}]
            }
            
            # 2. Initialize Simulator
            simulator = ContextSimulator(endpoint=omlx_endpoint)
            
            # 3. Get analysis from OMLX
            analysis = await simulator.estimate_impact(mock_data)
            
            # 4. Render the report
            simulator.render_simulation_report(analysis)
            
        except Exception as e:
            console.print(f"[red]Simulation failed: {str(e)}[/red]")

    asyncio.run(run_simulation())

if __name__ == "__main__":
    app()
