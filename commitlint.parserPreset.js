const teams = ["ENG", "OPS", "CS", "MKT", "SLS"];

const prefixes = ["Ref", "References", "Part of", "Fixes", "Closes"];

const issuePrefixes = [
  ...prefixes.flatMap((prefix) => {
    return teams.map((team) => `${prefix} ${team}-`);
  }),
];

module.exports = {
  parserOpts: {
    issuePrefixes,
  },
};
