// DE-PATH-005: SHOULD limit resource types and nesting to 2-3 levels
export default function pathNestingDepth(input, opts, context) {
  if (typeof input !== 'string') return [];
  const maxSegments = (opts && opts.maxStaticSegments) || 3;

  const staticSegments = input
    .split('/')
    .filter(Boolean)
    .filter((seg) => !(seg.startsWith('{') && seg.endsWith('}')));

  if (staticSegments.length > maxSegments) {
    return [
      {
        message: `Path has ${staticSegments.length} static segments (raccomandato: max ${maxSegments}) — valuta di ridurre l'annidamento`,
        path: context.path,
      },
    ];
  }
  return [];
}
