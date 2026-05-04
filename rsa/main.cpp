#include <iostream>
#include <fstream>
#include <vector>
#include <cstring>
#include <cstdlib>
#include <cmath>

using namespace std;

// проверка, является ли число простым
bool isPrime(long long n) {
    if (n < 2) return false;
    if (n == 2) return true;
    if (n % 2 == 0) return false;
    for (long long i = 3; i * i <= n; i += 2) {
        if (n % i == 0) return false;
    }
    return true;
}

// наибольший общий делитель
long long gcd(long long a, long long b) {
    while (b != 0) {
        long long tmp = b;
        b = a % b;
        a = tmp;
    }
    return a;
}

// быстрое возведение в степень по модулю: a^z mod n
long long fastExp(long long a, long long z, long long n) {
    long long a1 = a;
    long long z1 = z;
    long long x = 1;
    a1 = a1 % n;
    while (z1 > 0) {
        while (z1 % 2 == 0) {
            z1 = z1 / 2;
            a1 = (a1 * a1) % n;
        }
        z1 = z1 - 1;
        x = (x * a1) % n;
    }
    return x;
}

// расширенный алгоритм Евклида
// находит x1, y1 такие что x1*a + y1*b = gcd(a,b)
// если a и b взаимно просты, то y1 — мультипликативное инверсное b по модулю a
long long euclidEx(long long a, long long b) {
    long long d0 = a, d1 = b;
    long long x0 = 1, x1 = 0;
    long long y0 = 0, y1 = 1;

    while (d1 > 1) {
        long long q = d0 / d1;
        long long d2 = d0 % d1;
        long long x2 = x0 - q * x1;
        long long y2 = y0 - q * y1;
        d0 = d1; d1 = d2;
        x0 = x1; x1 = x2;
        y0 = y1; y1 = y2;
    }

    if (y1 < 0)
        y1 += a;

    return y1;
}

// факторизация числа r (поиск множителей p и q)
bool factorize(long long r, long long &p, long long &q) {
    for (long long i = 2; i * i <= r; i++) {
        if (r % i == 0) {
            p = i;
            q = r / i;
            if (isPrime(p) && isPrime(q))
                return true;
        }
    }
    return false;
}

// ========== 1. ШИФРОВАНИЕ ==========
void encryptFile() {
    long long p, q, Kc;
    char inputFile[256], outputFile[256];

    cout << "=== RSA Encryption ===" << endl;
    cout << "Enter p (prime number): ";
    cin >> p;
    cout << "Enter q (prime number): ";
    cin >> q;
    cout << "Enter private key Kc: ";
    cin >> Kc;
    cout << "Enter input file name: ";
    cin >> inputFile;
    cout << "Enter output file name: ";
    cin >> outputFile;

    // проверки
    if (!isPrime(p)) {
        cerr << "Error: p = " << p << " is not a prime number!" << endl;
        return;
    }
    if (!isPrime(q)) {
        cerr << "Error: q = " << q << " is not a prime number!" << endl;
        return;
    }
    if (p == q) {
        cerr << "Error: p and q must be different!" << endl;
        return;
    }

    long long r = p * q;
    long long phi = (p - 1) * (q - 1);

    if (r < 256) {
        cerr << "Error: r = p*q = " << r << " is too small (must be >= 256 for byte encryption)!" << endl;
        return;
    }

    if (Kc <= 1 || Kc >= phi) {
        cerr << "Error: Kc must be in range (1, " << phi << ")!" << endl;
        return;
    }
    if (gcd(Kc, phi) != 1) {
        cerr << "Error: Kc = " << Kc << " and phi(r) = " << phi << " are not coprime!" << endl;
        return;
    }

    // вычисление открытого ключа Ko
    long long Ko = euclidEx(phi, Kc);

    cout << endl;
    cout << "RSA parameters:" << endl;
    cout << "  p = " << p << endl;
    cout << "  q = " << q << endl;
    cout << "  r = p*q = " << r << endl;
    cout << "  phi(r) = " << phi << endl;
    cout << "  Kc (private key) = " << Kc << endl;
    cout << "  Ko (public key) = " << Ko << endl;

    // проверка правильности вычисления Ko
    if ((Ko * Kc) % phi != 1) {
        cerr << "Error: public key computation failed!" << endl;
        return;
    }

    // чтение входного файла
    ifstream fin(inputFile, ios::binary);
    if (!fin) {
        cerr << "Error: cannot open file " << inputFile << endl;
        return;
    }

    ofstream fout(outputFile, ios::binary);
    if (!fout) {
        cerr << "Error: cannot open file " << outputFile << endl;
        return;
    }

    // побайтовое шифрование: каждый байт -> 16-битное значение (2 байта, big-endian)
    unsigned char byte;
    while (fin.read((char*)&byte, 1)) {
        long long encrypted = fastExp(byte, Ko, r);
        unsigned char high = (encrypted >> 8) & 0xFF;
        unsigned char low = encrypted & 0xFF;
        fout.write((char*)&high, 1);
        fout.write((char*)&low, 1);
    }

    fin.close();
    fout.close();

    cout << "Encryption complete. Result saved to " << outputFile << endl;
}

// ========== 2. РАСШИФРОВАНИЕ ==========
void decryptFile() {
    long long r, Kc;
    char inputFile[256], outputFile[256];

    cout << "=== RSA Decryption ===" << endl;
    cout << "Enter modulus r: ";
    cin >> r;
    cout << "Enter private key Kc: ";
    cin >> Kc;
    cout << "Enter input file name: ";
    cin >> inputFile;
    cout << "Enter output file name: ";
    cin >> outputFile;

    if (r < 256) {
        cerr << "Error: r = " << r << " is too small!" << endl;
        return;
    }
    if (Kc <= 0) {
        cerr << "Error: Kc must be positive!" << endl;
        return;
    }

    ifstream fin(inputFile, ios::binary);
    if (!fin) {
        cerr << "Error: cannot open file " << inputFile << endl;
        return;
    }

    ofstream fout(outputFile, ios::binary);
    if (!fout) {
        cerr << "Error: cannot open file " << outputFile << endl;
        return;
    }

    // каждый 16-битный блок (2 байта) -> расшифровываем в 1 байт
    unsigned char high, low;
    while (fin.read((char*)&high, 1) && fin.read((char*)&low, 1)) {
        long long encrypted = (high << 8) | low;
        long long decrypted = fastExp(encrypted, Kc, r);
        unsigned char byte = decrypted & 0xFF;
        fout.write((char*)&byte, 1);
    }

    fin.close();
    fout.close();

    cout << "Decryption complete. Result saved to " << outputFile << endl;
}

// ========== 3. ВЗЛОМ (ДЕШИФРОВАНИЕ) ==========
void crackFile() {
    long long r, Ko;
    char inputFile[256], outputFile[256];

    cout << "=== RSA Crack ===" << endl;
    cout << "Enter modulus r: ";
    cin >> r;
    cout << "Enter public key Ko: ";
    cin >> Ko;
    cout << "Enter input file name: ";
    cin >> inputFile;
    cout << "Enter output file name: ";
    cin >> outputFile;

    if (r < 256) {
        cerr << "Error: r = " << r << " is too small!" << endl;
        return;
    }

    // факторизация r для нахождения p и q
    long long p, q;
    if (!factorize(r, p, q)) {
        cerr << "Error: failed to factorize r = " << r << "!" << endl;
        return;
    }

    long long phi = (p - 1) * (q - 1);

    if (gcd(Ko, phi) != 1) {
        cerr << "Error: Ko and phi(r) are not coprime!" << endl;
        return;
    }

    // вычисление закрытого ключа из открытого
    long long Kc = euclidEx(phi, Ko);

    cout << endl;
    cout << "Crack results:" << endl;
    cout << "  r = " << r << endl;
    cout << "  Found factors: p = " << p << ", q = " << q << endl;
    cout << "  phi(r) = " << phi << endl;
    cout << "  Ko (public key) = " << Ko << endl;
    cout << "  Kc (computed private key) = " << Kc << endl;

    ifstream fin(inputFile, ios::binary);
    if (!fin) {
        cerr << "Error: cannot open file " << inputFile << endl;
        return;
    }

    ofstream fout(outputFile, ios::binary);
    if (!fout) {
        cerr << "Error: cannot open file " << outputFile << endl;
        return;
    }

    // дешифрование аналогично расшифрованию, но с вычисленным Kc
    unsigned char high, low;
    while (fin.read((char*)&high, 1) && fin.read((char*)&low, 1)) {
        long long encrypted = (high << 8) | low;
        long long decrypted = fastExp(encrypted, Kc, r);
        unsigned char byte = decrypted & 0xFF;
        fout.write((char*)&byte, 1);
    }

    fin.close();
    fout.close();

    cout << "Crack complete. Result saved to " << outputFile << endl;
}

int main(int argc, char* argv[]) {
    if (argc != 2) {
        cout << "Usage:" << endl;
        cout << "  " << argv[0] << " encrypt   - encryption (input: p, q, Kc)" << endl;
        cout << "  " << argv[0] << " decrypt   - decryption (input: r, Kc)" << endl;
        cout << "  " << argv[0] << " crack     - crack/decipher (input: r, Ko)" << endl;
        return 1;
    }

    string mode = argv[1];

    if (mode == "encrypt") {
        encryptFile();
    } else if (mode == "decrypt") {
        decryptFile();
    } else if (mode == "crack") {
        crackFile();
    } else {
        cerr << "Error: unknown mode '" << mode << "'." << endl;
        return 1;
    }

    return 0;
}
