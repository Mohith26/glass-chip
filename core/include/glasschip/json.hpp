// Minimal JSON-like value tree used to pass configs (SoC yaml, fidelity yaml,
// task graphs) into the C++ core without depending on pybind11. The Python
// bindings convert dicts into this type; C++ tests parse JSON text into it.
#pragma once
#include <cstdint>
#include <string>
#include <utility>
#include <vector>

namespace gc {

struct Json {
  enum Type : uint8_t { Null, Bool, Num, Str, Arr, Obj };
  Type t = Null;
  bool b = false;
  double n = 0.0;
  std::string s;
  std::vector<Json> a;
  std::vector<std::pair<std::string, Json>> o;

  static Json number(double v) { Json j; j.t = Num; j.n = v; return j; }
  static Json string(std::string v) { Json j; j.t = Str; j.s = std::move(v); return j; }
  static Json boolean(bool v) { Json j; j.t = Bool; j.b = v; return j; }
  static Json array() { Json j; j.t = Arr; return j; }
  static Json object() { Json j; j.t = Obj; return j; }

  bool is_null() const { return t == Null; }
  bool is_num() const { return t == Num || t == Bool; }
  bool is_str() const { return t == Str; }
  bool is_arr() const { return t == Arr; }
  bool is_obj() const { return t == Obj; }

  // Returns the member named `key`, or nullptr if missing (or not an object).
  const Json* get(const std::string& key) const;
  // Sets (or replaces) a member; turns a Null into an object.
  Json& set(const std::string& key, Json v);
  void push(Json v) { if (t == Null) t = Arr; a.push_back(std::move(v)); }

  double as_num(double def = 0.0) const;
  // Convenience lookups with defaults.
  double num(const std::string& key, double def) const;
  std::string str(const std::string& key, const std::string& def) const;
  bool flag(const std::string& key, bool def) const;
  size_t size() const { return t == Arr ? a.size() : (t == Obj ? o.size() : 0); }
};

// Parses JSON text. Throws std::runtime_error on malformed input.
Json parse_json(const std::string& text);
// Serializes to compact JSON text (used for .gct footers and debugging).
std::string dump_json(const Json& j);

}  // namespace gc
