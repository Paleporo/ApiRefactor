// DE-STATUS-005: MUST support Problem JSON for errors (RFC 9457) con 'code' obbligatorio
// Input: l'oggetto "responses" di una singola operation
export default function problemDetailsErrors(input, opts, context) {
  if (!input || typeof input !== 'object') return [];
  const results = [];

  for (const [status, response] of Object.entries(input)) {
    if (!/^[45]\d\d$/.test(status)) continue;
    const content = response && response.content;

    if (!content || !content['application/problem+json']) {
      results.push({
        message: `Response ${status} deve usare 'application/problem+json' (RFC 9457), non un media type generico`,
        path: [...context.path, status, 'content'],
      });
      continue;
    }

    const schema = content['application/problem+json'].schema || {};
    const props = schema.properties || {};
    const requiredFields = ['type', 'title', 'status', 'code'];

    for (const field of requiredFields) {
      if (!(field in props)) {
        results.push({
          message: `Problem Details della response ${status} manca della proprieta' obbligatoria '${field}'`,
          path: [...context.path, status, 'content', 'application/problem+json', 'schema', 'properties'],
        });
      }
    }
  }
  return results;
}
