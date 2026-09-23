// DE-PATH-002: MUST use kebab-case for path segments
// Input: la chiave del path, es. "/payment-instructions/{payment-instruction-id}"
export default function kebabCasePath(input, opts, context) {
  if (typeof input !== 'string') return [];
  const results = [];
  const segments = input.split('/').filter(Boolean);

  for (const segment of segments) {
    if (segment.startsWith('{') && segment.endsWith('}')) {
      const paramName = segment.slice(1, -1);
      if (!/^[a-z][a-z0-9-]*$/.test(paramName)) {
        results.push({
          message: `Path parameter '{${paramName}}' must be kebab-case (es. 'payment-instruction-id')`,
          path: context.path,
        });
      }
    } else if (!/^[a-z][a-z0-9-]*$/.test(segment)) {
      results.push({
        message: `Path segment '${segment}' must be kebab-case (es. 'payment-instructions')`,
        path: context.path,
      });
    }
  }
  return results;
}
