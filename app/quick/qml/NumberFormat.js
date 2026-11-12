.pragma library

function compact(value, digits) {
    if (value === null || value === undefined || value === "") return "—"
    var n = Number(value)
    if (!isFinite(n)) return typeof value === "number" || /^[-+]?\s*(nan|infinity|inf)$/i.test(String(value)) ? "—" : String(value)
    var precision = digits === undefined ? 6 : Math.max(1, Math.min(15, digits))
    if (n === 0) return "0"
    if (Math.abs(n) >= 1e6 || Math.abs(n) < 1e-4) {
        var parts = n.toExponential(precision - 1).split("e")
        var coefficient = parts[0].replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "")
        return coefficient + "e" + parts[1]
    }
    var rounded = Number(n.toPrecision(precision))
    return String(rounded)
}

function quantity(value, unit, digits) {
    if (value === null || value === undefined || value === "") return "—"
    var n = Number(value)
    if (!isFinite(n)) return compact(value, digits)
    var suffix = unit || ""
    if (["A", "V", "T"].indexOf(suffix) >= 0 && n !== 0) {
        var magnitude = Math.abs(n)
        var scales = [[1e9, "G"], [1e6, "M"], [1e3, "k"], [1, ""], [1e-3, "m"], [1e-6, "µ"], [1e-9, "n"], [1e-12, "p"]]
        for (var i = 0; i < scales.length; ++i) {
            if (magnitude >= scales[i][0]) return compact(n / scales[i][0], digits) + " " + scales[i][1] + suffix
        }
    }
    return compact(n, digits) + (suffix ? " " + suffix : "")
}
