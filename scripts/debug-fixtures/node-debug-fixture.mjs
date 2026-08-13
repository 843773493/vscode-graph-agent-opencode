const input = Number(process.argv[2] ?? "7");
const label = "node-debug-fixture";

function transform(value) {
  const doubled = value * 2;
  const result = { value, doubled, label };
  return result;
}

function main() {
  const snapshot = transform(input);
  console.log(JSON.stringify(snapshot));
}

main();
process.exit(0);
