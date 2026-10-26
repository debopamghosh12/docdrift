# Items API

## Configuration

| Variable   | Default | Description |
|------------|---------|-------------|
| `GREETING` | `hello` | Greeting returned by `GET /greeting`. |

## Endpoints

```bash
curl http://localhost:8000/greeting
```

`200 OK`
```json
{"greeting": "hello"}
```

### Create an item

```bash
curl -X POST http://localhost:8000/items \
  -H "Content-Type: application/json" \
  -d '{"name": "Pen", "price": 1.5}'
```

`200 OK`
```json
{"id": 7, "name": "Pen", "price": 1.5, "created_at": "2024-01-01T00:00:00Z"}
```

### Get an item

```bash
curl http://localhost:8000/items/1
```

`200 OK`
```json
{"id": 1, "name": "Pen", "price": 1.5, "created_at": "2024-01-01T00:00:00Z"}
```

`404 Not Found` if it does not exist.
