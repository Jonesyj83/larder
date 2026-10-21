# Larder MCP server

A standalone stdio MCP server that exposes Larder's REST API as tools for your MCP client. Run it separately from the web application.

Set both required environment variables:

- `LARDER_URL`: the application URL, such as `http://127.0.0.1:8000` (include any deployment path prefix).
- `LARDER_API_KEY`: a personal API key created in Larder's Settings page. Every request sends it in the `x-api-key` header.

From the repository root, use the supplied MCP environment:

```sh
export LARDER_URL=http://127.0.0.1:8000
export LARDER_API_KEY=your-personal-key
.venv-mcp/bin/python mcp_server/server.py
```

With that environment activated, the command is `python mcp_server/server.py`.
The server supports SDK versions exposing `FastMCP` and version 2's renamed `MCPServer`.

Example JSON configuration for your MCP client (replace the absolute paths and key):

```json
{
  "mcpServers": {
    "larder": {
      "command": "/absolute/path/to/larder/.venv-mcp/bin/python",
      "args": ["/absolute/path/to/larder/mcp_server/server.py"],
      "env": {
        "LARDER_URL": "http://127.0.0.1:8000",
        "LARDER_API_KEY": "your-personal-key"
      }
    }
  }
}
```

Names resolve to existing products, locations and units using the REST catalogue. Product operations require an exact, unambiguous name. Creating a new product through `add_stock` requires an explicit unit. Purchase prices are total costs for each purchase. Dates use `YYYY-MM-DD`.

`save_recipe` ingredients follow the REST format: `amount`, `product_id` or `name`, `unit_id`, and optional `note`. A textual `unit` can replace `unit_id`. Recipe and meal saving do not purchase stock.

`test_server.py` is a standalone manual smoke test requiring a live Larder instance and both environment variables. It is deliberately outside the main unittest suite. It reads stock, catalogues, recipes, planning, memory and spending; it temporarily creates a household note and deletes it in a `finally` block. Run it with:

```sh
.venv-mcp/bin/python mcp_server/test_server.py
```
