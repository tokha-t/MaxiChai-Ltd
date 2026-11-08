// Число ответов показываем отдельно от запрошенного режима и согласия моделей.
const count = (value) => Number.isInteger(value) && value >= 0 ? value : null;

export function checksSummary(record = {}, requestedFallback = null) {
  const requested = count(record.requested_votes) ?? count(requestedFallback) ?? count(record.votes);
  const received = count(record.received_votes);
  const valid = count(record.valid_votes);
  const incomplete = record.votes_complete === false || (valid !== null && requested !== null && valid < requested);
  const verb = valid === 1 ? "Получена" : valid !== null && valid % 10 >= 2 && valid % 10 <= 4 && (valid % 100 < 12 || valid % 100 > 14) ? "Получены" : "Получено";
  const text = valid !== null && requested !== null
    ? `${verb} ${valid} из ${requested} проверок`
    : `Число валидных проверок не сохранено${requested !== null ? `; запрошено ${requested}` : ""}`;
  return { requested, received, valid, incomplete, text };
}

export function profileId(value) {
  if (!["string", "number"].includes(typeof value)) return null;
  const number = Number(value);
  return Number.isSafeInteger(number) && number > 0 ? number : null;
}

export function selectedProfile(profiles, value) {
  const id = profileId(value);
  return profiles.find((profile) => profile.id === id) || null;
}
