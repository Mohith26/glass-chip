// Byte serialization used by checkpoint()/restore().
//
// Every state struct exposes `template <class Ar> void io(Ar& ar)` that lists
// its fields once (GC_IO macro). The same list drives both writing and
// reading, so a field cannot be saved but forgotten on restore.
#pragma once
#include <array>
#include <cstdint>
#include <cstring>
#include <deque>
#include <map>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

namespace gc {

struct Writer {
  std::vector<uint8_t> buf;
  static constexpr bool reading = false;
  void raw(const void* p, size_t n) {
    const auto* b = static_cast<const uint8_t*>(p);
    buf.insert(buf.end(), b, b + n);
  }
};

struct Reader {
  const uint8_t* p = nullptr;
  size_t n = 0, pos = 0;
  static constexpr bool reading = true;
  void raw(void* dst, size_t k) {
    if (pos + k > n) throw std::runtime_error("checkpoint blob truncated");
    std::memcpy(dst, p + pos, k);
    pos += k;
  }
};

template <class Ar, class T>
void sv(Ar& ar, T& v);

template <class Ar, class T>
inline void sv_arith(Ar& ar, T& v) {
  if constexpr (Ar::reading) ar.raw(&v, sizeof(T));
  else ar.raw(&v, sizeof(T));
}

template <class T, class = void>
struct has_io : std::false_type {};
template <class T>
struct has_io<T, std::void_t<decltype(std::declval<T&>().io(std::declval<Writer&>()))>> : std::true_type {};

template <class Ar>
inline void sv(Ar& ar, std::string& s) {
  uint64_t n = s.size();
  sv_arith(ar, n);
  if constexpr (Ar::reading) s.resize(n);
  if (n) ar.raw(s.data(), n);
}

template <class Ar, class T>
inline void sv(Ar& ar, std::vector<T>& v) {
  uint64_t n = v.size();
  sv_arith(ar, n);
  if constexpr (Ar::reading) v.resize(n);
  if constexpr (std::is_arithmetic_v<T> || std::is_enum_v<T>) {
    if (n) ar.raw(v.data(), n * sizeof(T));
  } else {
    for (auto& x : v) sv(ar, x);
  }
}

template <class Ar, class T>
inline void sv(Ar& ar, std::deque<T>& v) {
  uint64_t n = v.size();
  sv_arith(ar, n);
  if constexpr (Ar::reading) v.resize(n);
  for (auto& x : v) sv(ar, x);
}

template <class Ar, class T, size_t N>
inline void sv(Ar& ar, std::array<T, N>& v) {
  for (auto& x : v) sv(ar, x);
}

template <class Ar, class A, class B>
inline void sv(Ar& ar, std::pair<A, B>& v) {
  sv(ar, v.first);
  sv(ar, v.second);
}

template <class Ar, class K, class V>
inline void sv(Ar& ar, std::map<K, V>& m) {
  uint64_t n = m.size();
  sv_arith(ar, n);
  if constexpr (Ar::reading) {
    m.clear();
    for (uint64_t i = 0; i < n; ++i) {
      K k{};
      V v{};
      sv(ar, k);
      sv(ar, v);
      m.emplace(std::move(k), std::move(v));
    }
  } else {
    for (auto& kv : m) {
      K k = kv.first;
      sv(ar, k);
      sv(ar, kv.second);
    }
  }
}

template <class Ar, class T>
inline void sv(Ar& ar, T& v) {
  if constexpr (std::is_arithmetic_v<T> || std::is_enum_v<T>) {
    sv_arith(ar, v);
  } else if constexpr (std::is_same_v<T, __uint128_t>) {
    sv_arith(ar, v);
  } else {
    v.io(ar);
  }
}

template <class Ar>
inline void sv_all(Ar&) {}
template <class Ar, class T, class... Rest>
inline void sv_all(Ar& ar, T& first, Rest&... rest) {
  sv(ar, first);
  sv_all(ar, rest...);
}

}  // namespace gc

#define GC_IO(...)                 \
  template <class Ar>              \
  void io(Ar& ar) {                \
    ::gc::sv_all(ar, __VA_ARGS__); \
  }
