// DE-STATUS-002: MUST specify success and error responses (almeno un 2xx)
export default function requiresSuccessResponse(input, opts, context) {
  if (!input || typeof input !== 'object') return [];
  const codes = Object.keys(input);
  const hasSuccess = codes.some((c) => /^2\d\d$/.test(c) || c === 'default');

  if (!hasSuccess) {
    return [
      {
        message: "L'operation deve definire almeno una response di successo (2xx)",
        path: context.path,
      },
    ];
  }
  return [];
}
