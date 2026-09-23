// DE-FMT-005: SHOULD name date/time properties with indicators (Date, Day, Time, Timestamp, o suffisso "At")
export default function dateTimePropertyNaming(input, opts, context) {
  if (!input || typeof input !== 'object') return [];
  const results = [];
  const indicatorPattern = /(Date|Day|Time|Timestamp)$|At$/;

  for (const [propName, propSchema] of Object.entries(input)) {
    if (!propSchema || typeof propSchema !== 'object') continue;
    const format = propSchema.format;
    if ((format === 'date' || format === 'date-time') && !indicatorPattern.test(propName)) {
      results.push({
        message: `Proprieta' '${propName}' ha format '${format}' ma il nome non lo indica (usa Date/Day/Time/Timestamp o suffisso 'At')`,
        path: [...context.path, propName],
      });
    }
  }
  return results;
}
