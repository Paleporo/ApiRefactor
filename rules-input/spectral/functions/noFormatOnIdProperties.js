// DE-FMT-004: SHOULD use UUIDs only when necessary / avoid defining a format for ID properties
// Input: un oggetto "properties" di uno schema
export default function noFormatOnIdProperties(input, opts, context) {
  if (!input || typeof input !== 'object') return [];
  const results = [];

  for (const [propName, propSchema] of Object.entries(input)) {
    const isIdLike = /Id$/.test(propName) || propName === 'id';
    if (isIdLike && propSchema && typeof propSchema === 'object' && 'format' in propSchema) {
      results.push({
        message: `Proprieta' identificativa '${propName}' non dovrebbe definire 'format' — usa string semplice per flessibilita' futura`,
        path: [...context.path, propName, 'format'],
      });
    }
  }
  return results;
}
