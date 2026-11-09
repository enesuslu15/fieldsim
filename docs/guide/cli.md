# Command line

```console
$ fieldsim --help
```

## `fieldsim init`

Write a starter file.

```bash
fieldsim init plant.yaml --protocol opcua     # modbus (default), opcua or s7
fieldsim init plant.yaml --force              # overwrite an existing file
```

## `fieldsim validate`

Check one or more files without starting anything. Exits 1 if any is invalid.

```console
$ fieldsim validate examples/*.yaml
OK    examples/boiler.yaml: 1 device, 7 points
OK    examples/packaging_line.yaml: 1 device, 6 points
OK    examples/press.yaml: 1 device, 6 points
```

## `fieldsim run`

Start every device of a file and serve until Ctrl+C.

| Option | |
|---|---|
| `--host 0.0.0.0` | Listen on this address for every device (containers). |
| `--duration 30` | Stop after 30 seconds. |
| `--log-level debug` | Show every request and fault. |

## `fieldsim check`

Exit 0 once every device of the file accepts TCP connections. Used as the
Docker health check and to wait for a simulator in CI.

```bash
fieldsim check plant.yaml --wait 30          # keep retrying for up to 30 s
fieldsim check plant.yaml --host plant       # connect to another host
```

## `fieldsim schema`

Print the JSON Schema of the file format, or write it with `-o`.
