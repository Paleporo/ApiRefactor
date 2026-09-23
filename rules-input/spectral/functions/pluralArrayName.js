// DE-JSON-006: MUST pluralize array names (euristica: il nome deve terminare con 's')
// Nota: euristica semplice, puo' generare qualche falso positivo/negativo su
// plurali irregolari (es. "children") — severity 'warn', non 'error'.
export default function pluralArrayName(input, opts, context) {
  if (!input || typeof input !== 'object') return [];
  const results = [];

  for (const [propName, propSchema] of Object.entries(input)) {
    if (!propSchema || typeof propSchema !== 'object') continue;
    if (propSchema.type === 'array' && !/s$/i.test(propName)) {
      results.push({
        message: `Proprieta' array '${propName}' dovrebbe avere nome plurale (es. '${propName}s')`,
        path: [...context.path, propName],
      });
    }
  }
  return results;
}
