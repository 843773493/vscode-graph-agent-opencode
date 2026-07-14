export function matches(value) {
  const pattern = "^foo\\d+$";
  return new RegExp(pattern).test(value);
}
